import requests
import json
import datetime
from urllib.parse import quote
from threading import Lock
from . import util

from django.http import HttpRequest

def mutex(lock):
    def decorated_func(func):
        def exclusive_func(*args, **kwargs):
            with lock:
                return func(*args, **kwargs)
        return exclusive_func
    return decorated_func

class RedirectState(object):
    openid_metadata_cache = {}

    config = {}
    config_lock = Lock()
        
    waiting = []
    waiting_lock = Lock()


'''
    This is the top level handler of authorization redirects and authorization url generation.

    For non-standard APIs which do not conform to OAuth 2.0 (specifically RFC 6749 sec 4.1), extensions may be required.
    (RFC 6749 sec 4.1.1 https://tools.ietf.org/html/rfc6749#section-4.1.1)
    Example: Dropbox APIv2 does not completely conform to RFC 6749#4.1.1 (authorization) nor 6749#4.1.3 (token exchange)

    State/nonce values in the urls generated by this class must not be modified by the client application or end user.
    Requests received which do not match state/nonce values generated by this class will be rejected.

    Does not yet support webfinger OpenID issuer discovery/specification.
'''
#TODO SSL Cert verification on all http requests. Force SSL on urls.
# Attempt to autodetect cacert location based on os, otherwise pull Mozilla's https://curl.haxx.se/ca/cacert.pem
# also look at default ssl verification in requests package, and in pyoidc package, could rely on them
class RedirectHandler(object):
    # static vars

    @mutex(RedirectState.config_lock)
    def __init__(self, config=None):
        if config:
            RedirectState.config.update(config)

        # timeout in seconds for authorization callbacks to be received
        # default is 300 (5 minutes)
        if 'authorization_timeout' in RedirectState.config:
            self.authorization_timeout = int(RedirectState.config['authorization_timeout'])
        else:
            self.authorization_timeout = 60 * 5

    
    '''
        uid: unique user identifier
        scopes: iterable of strings, used by OAuth2 and OpenID. If requesting authentication
                    via an OpenID provider, this must include 'openid'.
        provider_tag: matched against provider dictionary keys in the configuration loaded at startup
    '''
    @mutex(RedirectState.waiting_lock)
    def add(self, uid, scopes, provider_tag):
        # prevent unlikely chance that two waiting authorizations have same nonce or state
        # TODO change this to have in-memory cache of old nonce and state values. keep for N days
        while True:
            nonce = util.generate_nonce(64) # url safe 32bit (64byte hex)
            if not self.exists_nonce(nonce):
                break
        while True:
            state = util.generate_nonce(64)
            if not self.exists_state(state):
                break

        url = self._generate_authorization_url(state, nonce, scopes, provider_tag)
        RedirectState.waiting.append(
            {
                'uid': uid,
                'state': state,
                'nonce': nonce,
                'scopes': scopes,
                'provider': provider_tag,
                'ctime': datetime.datetime.now()
            }
        )
        return url


    '''
        request is a django.http.HttpRequest
    '''
    @mutex(RedirectState.waiting_lock)
    def accept(self, request):
        pass


    def exists_state(self, state):
        return self.get_from_state(state) != None

    def get_from_state(self, state):
        l = self.get_from_field('state', state)
        if len(l) != 1: return None
        else: return l[0]

    def exists_nonce(self, nonce):
        return self.get_from_nonce(nonce) != None

    def get_from_nonce(self, nonce):
        l = self.get_from_field('nonce', nonce)
        if len(l) != 1: return None
        else: return l[0]

    def get_from_field(self, fieldname, fieldval):
        return [x for x in self.waiting if x[fieldname] == fieldval]
        
    '''
        Not thread-safe, should only be used internally
    '''
    def _generate_authorization_url(self, state, nonce, scopes, provider_tag):
        p = self.config['providers'][provider_tag]
        
        client_id = p['client_id']
        redirect_uri = self.config['redirect_uri']

        # get auth endpoint
        if p['standard'] == 'OpenID Connect':
            # openid allowed for endpoint and other value specification within metadata file
            meta_url = p['metadata_url']
            if provider_tag not in RedirectState.openid_metadata_cache:
                response = requests.get(meta_url)
                if response.code != 200:
                    raise RuntimeError('could not retrieve openid metadata, returned error: ' + str(response.code))
                # cache this metadata
                RedirectState.openid_metadata_cache[provider_tag] = meta
                meta = json.loads(response.content.decode('utf-8'))
            else:
                meta = RedirectState.openid_metadata_cache[provider_tag]

            authorization_endpoint = meta['authorization_endpoint']
            if ',' in scope:
                scope = ' '.join(scope.split(','))
            scope = quote(scope)
            additional_params += 'scope=' + scope
            additional_params += '&response_type=code'
            additional_params += '&access_type=offline'
            additional_params += '&login%20consent'

        elif p['standard'] == 'OAuth 2.0':
            authorization_endpoint = p['authorization_endpoint']
            additional_params = ''
            if 'additional_params' in p:
                additional_params = p['additional_params']

        else:
            raise RuntimeError('unknown provider standard: ' + p['standard'])

        url = '{}?state={}&nonce={}&redirect_uri={}client_id={}&{}'.format(
            authorization_endpoint,
            state,
            nonce,
            redirect_uri,
            client_id,
            additonal_params,
        )
        return url