""" S3-backed pypi server """
import calendar
import datetime
import logging
from pyramid.config import Configurator
from pyramid.renderers import JSON, render
from pyramid.settings import asbool
from pyramid_beaker import session_factory_from_settings
from six.moves.urllib.parse import urlencode  # pylint: disable=F0401,E0611

from .route import Root
from .util import BetterScrapingLocator


__version__ = "1.0.10"
LOG = logging.getLogger(__name__)


def to_json(value):
    """ A json filter for jinja2 """
    return render("json", value)


def _app_url(request, *paths, **params):
    """ Get the base url for the root of the app plus an optional path """
    path = "/".join(paths)
    if not path.startswith("/"):
        path = "/" + path
    if params:
        path += "?" + urlencode(params)
    return request.application_url + path


def _fallback_simple(request):
    """ /simple endpoint of the fallback url """
    if request.registry.fallback_url is not None:
        return request.registry.fallback_url
    else:
        return "/".join([request.registry.fallback_base_url, "simple"])


def _locator(request):
    """ Get the scraping locator to find packages from the fallback site """
    return BetterScrapingLocator(request.fallback_simple)


def _add_postfork_hook(config, hook):
    """ Add a postfork hook """
    config.registry.postfork_hooks.append(hook)


def includeme(config):
    """ Set up and configure the pypicloud app """
    config.set_root_factory(Root)
    settings = config.get_settings()
    config.add_route("health", "/health")
    config.include("pyramid_tm")
    # Beaker should be set by default to invalidate corrupt sessions, otherwise
    # a bad cookie will break the website for you and the only fix is to
    # manually delete the cookie.
    settings.setdefault("session.invalidate_corrupt", "true")
    config.include("pyramid_beaker")
    config.include("pyramid_duh")
    config.include("pyramid_duh.auth")
    config.include("pyramid_rpc.xmlrpc")

    # Sometimes we need to run things after uWSGI forks.
    config.registry.postfork_hooks = []
    config.add_directive("add_postfork_hook", _add_postfork_hook)
    try:
        from uwsgidecorators import postfork
    except ImportError:
        pass
    else:

        @postfork
        def run_postfork_hooks():
            """ Run hooks after uWSGI forks """
            for fn in config.registry.postfork_hooks:
                fn()

    config.include("pypicloud.auth")
    config.include("pypicloud.access")
    config.include("pypicloud.cache")

    # If we're reloading templates, we should also pretty-print json
    reload_templates = asbool(settings.get("pyramid.reload_templates"))
    indent = 4 if reload_templates else None
    json_renderer = JSON(indent=indent)
    json_renderer.add_adapter(
        datetime.datetime, lambda obj, r: calendar.timegm(obj.utctimetuple())
    )
    config.add_renderer("json", json_renderer)
    # Jinja2 configuration
    settings["jinja2.filters"] = {
        "static_url": "pyramid_jinja2.filters:static_url_filter",
        "tojson": to_json,
    }
    settings["jinja2.directories"] = ["pypicloud:templates"]
    config.include("pyramid_jinja2")

    # BEAKER CONFIGURATION
    config.registry.secure_cookie = asbool(settings.get("session.secure", False))
    settings.setdefault("session.type", "cookie")
    settings.setdefault("session.httponly", "true")
    settings.setdefault("session.crypto_type", "cryptography")
    config.set_session_factory(session_factory_from_settings(settings))

    # PYPICLOUD SETTINGS
    if "pypi.fallback_base_url" in settings:
        config.registry.fallback_base_url = settings["pypi.fallback_base_url"].rstrip(
            "/"
        )
        config.registry.fallback_url = None
    elif "pypi.fallback_url" in settings:
        LOG.warning(
            "pypi.fallback_url is deprecated and support will be "
            "removed in a future version. "
            "Please use pypi.fallback_base_url."
        )
        config.registry.fallback_url = settings["pypi.fallback_url"]
        config.registry.fallback_base_url = None
    else:
        config.registry.fallback_base_url = "https://pypi.python.org"
        config.registry.fallback_url = None
    config.add_request_method(_fallback_simple, name="fallback_simple", reify=True)

    fallback_mode = settings.get("pypi.fallback", "redirect")
    always_show_upstream = settings.get("pypi.always_show_upstream")

    # Using fallback=mirror is the same as fallback=cache and
    # always_show_upstream=true
    if always_show_upstream is None:
        always_show_upstream = fallback_mode == "mirror"
    else:
        always_show_upstream = asbool(always_show_upstream)

    modes = ("redirect", "cache", "none")
    if fallback_mode not in modes:
        raise ValueError(
            "Invalid value for 'pypi.fallback'. " "Must be one of %s" % ", ".join(modes)
        )
    config.registry.fallback = fallback_mode
    config.registry.always_show_upstream = always_show_upstream

    # Special request methods
    config.add_request_method(_app_url, name="app_url")
    config.add_request_method(_locator, name="locator", reify=True)
    config.add_request_method(
        lambda x: __version__, name="pypicloud_version", reify=True
    )
    config.add_request_method(
        lambda x: settings.get("pypi.download_url"),
        name="custom_download_url",
        reify=True,
    )

    cache_max_age = int(settings.get("pyramid.cache_max_age", 3600))
    config.add_static_view(
        name="static/%s" % __version__,
        path="pypicloud:static",
        cache_max_age=cache_max_age,
    )

    config.add_xmlrpc_endpoint(
        "pypi", "/pypi", request_method="POST", header="Content-Type:text/xml"
    )
    config.add_xmlrpc_endpoint(
        "pypi_slash", "/pypi/", request_method="POST", header="Content-Type:text/xml"
    )


def main(config, **settings):
    """ This function returns a Pyramid WSGI application.
    """
    config = Configurator(settings=settings)
    config.include("pypicloud")
    config.scan("pypicloud.views")
    return config.make_wsgi_app()
