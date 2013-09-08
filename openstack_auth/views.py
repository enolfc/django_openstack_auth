import logging

from threading import Thread
from urlparse import urljoin

from django import shortcuts
from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth import authenticate as django_auth
from django.contrib.auth import login as django_do_login
from django.contrib.auth.views import (login as django_login,
                                       logout_then_login as django_logout)
from django.contrib.auth.decorators import login_required
from django.views.decorators.debug import sensitive_post_parameters
from django.utils.functional import curry
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

try:
    from django.utils.http import is_safe_url
except ImportError:
    from .utils import is_safe_url

from keystoneclient.v2_0 import client as keystone_client
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient.httpclient import HTTPClient

from .forms import Login
from .user import set_session_from_user, create_user_from_token

from cas.views import _service_url, _login_url

LOG = logging.getLogger(__name__)


@sensitive_post_parameters()
@csrf_protect
@never_cache
def login(request):
    """ Logs a user in using the :class:`~openstack_auth.forms.Login` form. """
    # Get our initial region for the form.
    initial = {}
    current_region = request.session.get('region_endpoint', None)
    requested_region = request.GET.get('region', None)
    regions = dict(getattr(settings, "AVAILABLE_REGIONS", []))
    if requested_region in regions and requested_region != current_region:
        initial.update({'region': requested_region})

    if request.method == "POST":
        form = curry(Login, request)
    else:
        form = curry(Login, initial=initial)

    extra_context = {'redirect_field_name': REDIRECT_FIELD_NAME}

    if request.is_ajax():
        template_name = 'auth/_login.html'
        extra_context['hide'] = True
    else:
        template_name = 'auth/login.html'

    res = django_login(request,
                       template_name=template_name,
                       authentication_form=form,
                       extra_context=extra_context)
    # Set the session data here because django's session key rotation
    # will erase it if we set it earlier.
    if request.user.is_authenticated():
        set_session_from_user(request, request.user)
        regions = dict(Login.get_region_choices())
        region = request.user.endpoint
        region_name = regions.get(region)
        request.session['region_endpoint'] = region
        request.session['region_name'] = region_name
    return res


def logout(request):
    msg = 'Logging out user "%(username)s".' % \
        {'username': request.user.username}
    LOG.info(msg)
    if 'token_list' in request.session:
        t = Thread(target=delete_all_tokens,
                   args=(list(request.session['token_list']),))
        t.start()
    """ Securely logs a user out. """
    return django_logout(request)


def delete_all_tokens(token_list):
    insecure = getattr(settings, 'OPENSTACK_SSL_NO_VERIFY', False)
    for token_tuple in token_list:
        try:
            endpoint = token_tuple[0]
            token = token_tuple[1]
            client = keystone_client.Client(endpoint=endpoint,
                                            token=token,
                                            insecure=insecure)
            client.tokens.delete(token=token)
        except keystone_exceptions.ClientException as e:
            LOG.info('Could not delete token')


@login_required
def switch(request, tenant_id, redirect_field_name=REDIRECT_FIELD_NAME):
    """ Switches an authenticated user from one tenant to another. """
    LOG.debug('Switching to tenant %s for user "%s".'
              % (tenant_id, request.user.username))
    insecure = getattr(settings, 'OPENSTACK_SSL_NO_VERIFY', False)
    endpoint = request.user.endpoint
    client = keystone_client.Client(endpoint=endpoint,
                                    insecure=insecure)
    try:
        token = client.tokens.authenticate(tenant_id=tenant_id,
                                        token=request.user.token.id)
        msg = 'Tenant switch successful for user "%(username)s".' % \
            {'username': request.user.username}
        LOG.info(msg)
    except keystone_exceptions.ClientException:
        msg = 'Tenant switch failed for user "%(username)s".' % \
            {'username': request.user.username}
        LOG.warning(msg)
        token = None
        LOG.exception('An error occurred while switching sessions.')

    # Ensure the user-originating redirection url is safe.
    # Taken from django.contrib.auth.views.login()
    redirect_to = request.REQUEST.get(redirect_field_name, '')
    if not is_safe_url(url=redirect_to, host=request.get_host()):
        redirect_to = settings.LOGIN_REDIRECT_URL

    if token:
        user = create_user_from_token(request, token, endpoint)
        set_session_from_user(request, user)
    return shortcuts.redirect(redirect_to)


@login_required
def switch_region(request, region_name,
                  redirect_field_name=REDIRECT_FIELD_NAME):
    """
    Switches the non-identity services region that is being managed
    for the scoped project.
    """
    available_regs = getattr(settings, 'AVAILABLE_REGIONS', [])
    region_urls = [r[0] for r in available_regs if r[1] == region_name] 
    if not region_urls:
        # this should never happen, just redirect
        return django_logout(request)
    auth_url = region_urls[0]
    LOG.debug('Switching to %s at %s' % (region_name, auth_url))

    try:
        region_tokens = request.session['region_tokens']
        unscoped_token = region_tokens[auth_url]
    except KeyError:
        return django_logout(request)

    user = django_auth(request=request,
                       username=request.user.username,
                       password=None,
                       unscoped_token=unscoped_token, 
                       tenant=None,
                       auth_url=auth_url)
    django_do_login(request, user)
    if not request.user.is_authenticated():
        return django_logout(request)

    set_session_from_user(request, request.user)
    regions = dict(Login.get_region_choices())
    region = request.user.endpoint
    region_name = regions.get(region)
    request.session['region_endpoint'] = region
    request.session['region_name'] = region_name
    request.session['region_tokens'] = region_tokens 

    redirect_to = request.REQUEST.get(redirect_field_name, '')
    if not is_safe_url(url=redirect_to, host=request.get_host()):
        redirect_to = settings.LOGIN_REDIRECT_URL

    LOG.debug("Redirecting to %s" % redirect_to)
    return shortcuts.redirect(redirect_to)


def _retrieve_cas_token(auth_url, insecure, ticket, service):
    client = HTTPClient(auth_url=auth_url,
                        insecure=insecure)
    params = {"auth": {"casCredentials": {"ticket": ticket,
                                          "service": service}}}
    # XXX why this does not work?
    url = urljoin(auth_url, 'tokens')
    resp, body = client.request(auth_url + "/tokens", 'POST', body=params)
    return body['access']['token']['id']


def _retrieve_cas_url(auth_url, insecure, service):
    client = HTTPClient(auth_url=auth_url,
                        insecure=insecure)
    params = {"auth": {"casCredentials": {"service": service}}}
    # XXX why this does not work?
    url = urljoin(auth_url, 'tokens')
    resp, body = client.request(auth_url + "/tokens", 'POST', body=params)
    return body['cas_login_url']


def _unscoped_token_login(request, auth_url, unscoped_token,
                          region_tokens=None, 
                          redirect_field_name=REDIRECT_FIELD_NAME):
    user = django_auth(request=request,
                       username=None,
                       password=None,
                       unscoped_token=unscoped_token, 
                       tenant=None,
                       auth_url=auth_url)
    django_do_login(request, user)
    if not request.user.is_authenticated():
        return django_logout(request)

    set_session_from_user(request, request.user)
    regions = dict(Login.get_region_choices())
    region = request.user.endpoint
    region_name = regions.get(region)
    request.session['region_endpoint'] = region
    request.session['region_name'] = region_name
    if region_tokens:
        request.session['region_tokens'] = region_tokens 

    redirect_to = request.REQUEST.get(redirect_field_name, '')
    if not is_safe_url(url=redirect_to, host=request.get_host()):
        redirect_to = settings.LOGIN_REDIRECT_URL

    LOG.debug("Redirecting to %s" % redirect_to)
    return shortcuts.redirect(redirect_to)



def cas_login(request):
    ticket = request.REQUEST.get('ticket', None)
    service = _service_url(request, None, False)
    auth_url = settings.OPENSTACK_KEYSTONE_URL
    insecure = getattr(settings, 'OPENSTACK_SSL_NO_VERIFY', False)
    if ticket:
        token = _retrieve_cas_token(auth_url, insecure, ticket, service)
        return _unscoped_token_login(request, auth_url, token)
    else:
        return shortcuts.redirect(_retrieve_cas_url(auth_url,
                                                    insecure,
                                                    service))
