import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Security Settings
DEBUG = os.environ.get('DEBUG', 'False') == 'True'
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', "test-only-secret-key")
if not SECRET_KEY:
    if 'test' in sys.argv:
        SECRET_KEY = 'test-only-secret-key'
    else:
        raise ImproperlyConfigured('DJANGO_SECRET_KEY must be configured outside Django tests.')

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
    if host.strip()
]

# Application Definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'route_planner',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# CRITICAL FIX: Explicitly restore root routing wiring definitions
ROOT_URLCONF = 'fuel_optimizer_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'fuel_optimizer_project.wsgi'

# Persistent database storage mount volume configuration inside container
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db_volume' / 'db.sqlite3',
    }
}

# High-Performance Caching
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis_cache:6379/1')

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        }
    }
}

# Static asset configurations
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# External mapping providers are configurable so a production deployment can self-host or swap them.
NOMINATIM_BASE_URL = os.environ.get('NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')
NOMINATIM_USER_AGENT = os.environ.get('NOMINATIM_USER_AGENT', 'fuel-optimizer-api/1.0')
NOMINATIM_CONTACT_EMAIL = os.environ.get('NOMINATIM_CONTACT_EMAIL', '')
OSRM_BASE_URL = os.environ.get('OSRM_BASE_URL', 'https://router.project-osrm.org')
ROUTE_HTTP_TIMEOUT_SECONDS = float(os.environ.get('ROUTE_HTTP_TIMEOUT_SECONDS', '8'))
GEOCODE_CACHE_TTL_SECONDS = int(os.environ.get('GEOCODE_CACHE_TTL_SECONDS', '604800'))
ROUTE_CACHE_TTL_SECONDS = int(os.environ.get('ROUTE_CACHE_TTL_SECONDS', '86400'))
VEHICLE_RANGE_MILES = float(os.environ.get('VEHICLE_RANGE_MILES', '500'))
VEHICLE_MILES_PER_GALLON = float(os.environ.get('VEHICLE_MILES_PER_GALLON', '10'))
FUEL_RESERVE_MILES = float(os.environ.get('FUEL_RESERVE_MILES', '50'))
FUEL_STATION_CORRIDOR_MILES = float(os.environ.get('FUEL_STATION_CORRIDOR_MILES', '4'))
ORIGIN_PRICE_SEARCH_MILES = float(os.environ.get('ORIGIN_PRICE_SEARCH_MILES', '25'))

ROUTE_PLANNER_LOG_LEVEL = os.environ.get('ROUTE_PLANNER_LOG_LEVEL', 'INFO').upper()
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'route_planner': {
            'handlers': ['console'],
            'level': ROUTE_PLANNER_LOG_LEVEL,
            'propagate': False,
        },
    },
}
