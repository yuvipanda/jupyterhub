# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

from oauth2.error import ClientNotFoundError
from sqlalchemy import inspect
from tornado import gen
from tornado.log import app_log

from .utils import url_path_join, default_server_name

from . import orm
from ._version import _check_version, __version__
from .objects import Server
from traitlets import HasTraits, Any, Dict, observe, default
from .spawner import LocalProcessSpawner

class UserDict(dict):
    """Like defaultdict, but for users

    Getting by a user id OR an orm.User instance returns a User wrapper around the orm user.
    """
    def __init__(self, db_factory, settings):
        self.db_factory = db_factory
        self.settings = settings
        super().__init__()

    @property
    def db(self):
        return self.db_factory()

    def __contains__(self, key):
        if isinstance(key, (User, orm.User)):
            key = key.id
        return dict.__contains__(self, key)

    def __getitem__(self, key):
        if isinstance(key, User):
            key = key.id
        elif isinstance(key, str):
            orm_user = self.db.query(orm.User).filter(orm.User.name == key).first()
            if orm_user is None:
                raise KeyError("No such user: %s" % key)
            else:
                key = orm_user
        if isinstance(key, orm.User):
            # users[orm_user] returns User(orm_user)
            orm_user = key
            if orm_user.id not in self:
                user = self[orm_user.id] = User(orm_user, self.settings)
                return user
            user = dict.__getitem__(self, orm_user.id)
            user.db = self.db
            return user
        elif isinstance(key, int):
            id = key
            if id not in self:
                orm_user = self.db.query(orm.User).filter(orm.User.id == id).first()
                if orm_user is None:
                    raise KeyError("No such user: %s" % id)
                user = self[id] = User(orm_user, self.settings)
            return dict.__getitem__(self, id)
        else:
            raise KeyError(repr(key))

    def __delitem__(self, key):
        user = self[key]
        user_id = user.id
        db = self.db
        db.delete(user.orm_user)
        db.commit()
        dict.__delitem__(self, user_id)


class User(HasTraits):

    @default('log')
    def _log_default(self):
        return app_log

    settings = Dict()

    db = Any(allow_none=True)

    @default('db')
    def _db_default(self):
        if self.orm_user:
            return inspect(self.orm_user).session

    @observe('db')
    def _db_changed(self, change):
        """Changing db session reacquires ORM User object"""
        # db session changed, re-get orm User
        db = change.new
        if self._user_id is not None:
            self.orm_user = db.query(orm.User).filter(orm.User.id == self._user_id).first()
        self.spawner.db = change.new

    _user_id = None
    orm_user = Any(allow_none=True)
    @observe('orm_user')
    def _orm_user_changed(self, change):
        if change.new:
            self._user_id = change.new.id
        else:
            self._user_id = None

    spawner = None
    spawn_pending = False
    stop_pending = False
    proxy_pending = False
    waiting_for_response = False

    @property
    def authenticator(self):
        return self.settings.get('authenticator', None)

    @property
    def spawner_class(self):
        return self.settings.get('spawner_class', LocalProcessSpawner)

    def __init__(self, orm_user, settings=None, **kwargs):
        if settings:
            kwargs['settings'] = settings
        kwargs['orm_user'] = orm_user
        super().__init__(**kwargs)

        self.allow_named_servers = self.settings.get('allow_named_servers', False)

        self.base_url = url_path_join(
            self.settings.get('base_url', '/'), 'user', self.escaped_name) + '/'

        self.spawner = self.spawner_class(
            user=self,
            db=self.db,
            hub=self.settings.get('hub'),
            authenticator=self.authenticator,
            config=self.settings.get('config'),
        )

    # pass get/setattr to ORM user

    def __getattr__(self, attr):
        if hasattr(self.orm_user, attr):
            return getattr(self.orm_user, attr)
        else:
            raise AttributeError(attr)

    def __setattr__(self, attr, value):
        if not attr.startswith('_') and self.orm_user and hasattr(self.orm_user, attr):
            setattr(self.orm_user, attr, value)
        else:
            super().__setattr__(attr, value)

    def __repr__(self):
        return repr(self.orm_user)

    @property # FIX-ME CHECK IF STILL NEEDED
    def running(self):
        """property for whether a user has a fully running, accessible server"""
        if self.spawn_pending or self.stop_pending or self.proxy_pending:
            return False  # server is not running if spawn or stop is still pending
        if self.server is None:
            return False
        return True
    
    @property
    def server(self):
        if len(self.servers) == 0:
            return None
        else:
            return Server(orm_server=self.servers[0])

    @property
    def escaped_name(self):
        """My name, escaped for use in URLs, cookies, etc."""
        return quote(self.name, safe='@')

    @property
    def proxy_spec(self):
        if self.settings.get('subdomain_host'):
            return self.domain + self.base_url
        else:
            return self.base_url

    @property
    def domain(self):
        """Get the domain for my server."""
        # FIXME: escaped_name probably isn't escaped enough in general for a domain fragment
        return self.escaped_name + '.' + self.settings['domain']

    @property
    def host(self):
        """Get the *host* for my server (proto://domain[:port])"""
        # FIXME: escaped_name probably isn't escaped enough in general for a domain fragment
        parsed = urlparse(self.settings['subdomain_host'])
        h = '%s://%s.%s' % (parsed.scheme, self.escaped_name, parsed.netloc)
        return h

    @property
    def url(self):
        """My URL

        Full name.domain/path if using subdomains, otherwise just my /base/url
        """
        if self.settings.get('subdomain_host'):
            return '{host}{path}'.format(
                host=self.host,
                path=self.base_url,
            )
        else:
            return self.base_url

    @gen.coroutine
    def spawn(self, options=None):
        """Start the user's spawner
        
        depending from the value of JupyterHub.allow_named_servers
        
        if False:
        JupyterHub expects only one single-server per user
        url of the server will be /user/:name
        
        if True:
        JupyterHub expects more than one single-server per user
        url of the server will be /user/:name/:server_name
        """
        db = self.db
        if self.allow_named_servers:
            if options is not None and 'server_name' in options:
                server_name = options['server_name']
            else:
                server_name = default_server_name(self)
            base_url = url_path_join(self.base_url, server_name) + '/'
        else:
            server_name = ''
            base_url = self.base_url

        orm_server = orm.Server(
            name=server_name,
            base_url=base_url,
        )
        self.servers.append(orm_server)

        api_token = self.new_api_token()
        db.commit()

        server = Server(orm_server=orm_server)

        spawner = self.spawner
        # Passing server_name to the spawner
        spawner.server_name = server_name
        spawner.user_options = options or {}
        # we are starting a new server, make sure it doesn't restore state
        spawner.clear_state()

        # create API and OAuth tokens
        spawner.api_token = api_token
        spawner.admin_access = self.settings.get('admin_access', False)
        client_id = 'user-%s' % self.escaped_name
        if server_name:
            client_id = '%s-%s' % (client_id, server_name)
        spawner.oauth_client_id = client_id
        oauth_provider = self.settings.get('oauth_provider')
        if oauth_provider:
            client_store = oauth_provider.client_authenticator.client_store
            try:
                oauth_client = client_store.fetch_by_client_id(client_id)
            except ClientNotFoundError:
                oauth_client = None
            # create a new OAuth client + secret on every launch,
            # except for resuming containers.
            if oauth_client is None or not spawner.will_resume:
                client_store.add_client(client_id, api_token,
                                        url_path_join(self.url, 'oauth_callback'),
                                        )
        db.commit()

        # trigger pre-spawn hook on authenticator
        authenticator = self.authenticator
        if (authenticator):
            yield gen.maybe_future(authenticator.pre_spawn_start(self, spawner))

        self.spawn_pending = True
        # wait for spawner.start to return
        try:
            f = spawner.start()
            # commit any changes in spawner.start (always commit db changes before yield)
            db.commit()
            ip_port = yield gen.with_timeout(timedelta(seconds=spawner.start_timeout), f)
            if ip_port:
                # get ip, port info from return value of start()
                server.ip, server.port = ip_port
            else:
                # prior to 0.7, spawners had to store this info in user.server themselves.
                # Handle < 0.7 behavior with a warning, assuming info was stored in db by the Spawner.
                self.log.warning("DEPRECATION: Spawner.start should return (ip, port) in JupyterHub >= 0.7")
            if spawner.api_token != api_token:
                # Spawner re-used an API token, discard the unused api_token
                orm_token = orm.APIToken.find(self.db, api_token)
                if orm_token is not None:
                    self.db.delete(orm_token)
                    self.db.commit()
        except Exception as e:
            if isinstance(e, gen.TimeoutError):
                self.log.warning("{user}'s server failed to start in {s} seconds, giving up".format(
                    user=self.name, s=spawner.start_timeout,
                ))
                e.reason = 'timeout'
            else:
                self.log.error("Unhandled error starting {user}'s server: {error}".format(
                    user=self.name, error=e,
                ))
                e.reason = 'error'
            try:
                yield self.stop()
            except Exception:
                self.log.error("Failed to cleanup {user}'s server that failed to start".format(
                    user=self.name,
                ), exc_info=True)
            # raise original exception
            raise e
        spawner.start_polling()

        # store state
        self.state = spawner.get_state()
        self.last_activity = datetime.utcnow()
        db.commit()
        self.waiting_for_response = True
        try:
            resp = yield server.wait_up(http=True, timeout=spawner.http_timeout)
        except Exception as e:
            if isinstance(e, TimeoutError):
                self.log.warning(
                    "{user}'s server never showed up at {url} "
                    "after {http_timeout} seconds. Giving up".format(
                        user=self.name,
                        url=server.url,
                        http_timeout=spawner.http_timeout,
                    )
                )
                e.reason = 'timeout'
            else:
                e.reason = 'error'
                self.log.error("Unhandled error waiting for {user}'s server to show up at {url}: {error}".format(
                    user=self.name, url=server.url, error=e,
                ))
            try:
                yield self.stop()
            except Exception:
                self.log.error("Failed to cleanup {user}'s server that failed to start".format(
                    user=self.name,
                ), exc_info=True)
            # raise original TimeoutError
            raise e
        else:
            server_version = resp.headers.get('X-JupyterHub-Version')
            _check_version(__version__, server_version, self.log)
        finally:
            self.waiting_for_response = False
            self.spawn_pending = False
        return self

    @gen.coroutine
    def stop(self):
        """Stop the user's spawner

        and cleanup after it.
        """
        self.spawn_pending = False
        spawner = self.spawner
        self.spawner.stop_polling()
        self.stop_pending = True
        try:
            api_token = self.spawner.api_token
            status = yield spawner.poll()
            if status is None:
                yield self.spawner.stop()
            spawner.clear_state()
            self.state = spawner.get_state()
            self.last_activity = datetime.utcnow()
            # Cleanup defunct servers: delete entry and API token for each server
            for server in self.servers:
                # remove server entry from db
                self.db.delete(server)
            if not spawner.will_resume:
                # find and remove the API token if the spawner isn't
                # going to re-use it next time
                orm_token = orm.APIToken.find(self.db, api_token)
                if orm_token:
                    self.db.delete(orm_token)
            self.db.commit()
        finally:
            # trigger post-spawner hook on authenticator
            auth = spawner.authenticator
            try:
                if auth:
                    yield gen.maybe_future(
                        auth.post_spawn_stop(self, spawner)
                    )
            except Exception:
                self.log.exception("Error in Authenticator.post_spawn_stop for %s", self)
            self.stop_pending = False
