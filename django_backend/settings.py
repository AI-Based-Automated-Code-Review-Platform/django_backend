"""
Django settings for django_backend project.

Generated by 'django-admin startproject' using Django 4.0.

For more information on this file, see
https://docs.djangoproject.com/en/4.0/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/4.0/ref/settings/
"""

from pathlib import Path
from datetime import timedelta
import os

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/4.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-d-wn-ct8(#yoqw1wxw4%-4q%2+ten1qzgpwqz9)$kn%&q2oglu'

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = []


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'core.apps.CoreConfig',
    'rest_framework',  # Add this line
    'corsheaders',     # Add this line
    'django_celery_results',
    'django_celery_beat',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware', # Add this line (should be high up)
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'django_backend.urls'

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

WSGI_APPLICATION = 'django_backend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/4.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'codeRevieww',  # From your FastAPI config: POSTGRES_DB
        'USER': 'postgres',     # From your FastAPI config: POSTGRES_USER
        'PASSWORD': 'postgres', # From your FastAPI config: POSTGRES_PASSWORD
        'HOST': 'localhost',    # From your FastAPI config: POSTGRES_SERVER
        'PORT': '5432',         # Default PostgreSQL port
    }
}


# Password validation
# https://docs.djangoproject.com/en/4.0/ref/settings/#auth-password-validators

AUTH_USER_MODEL = 'core.User'  # Add this line

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/4.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.0/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/4.0/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS Configuration - Update with your actual origins
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173", # From your FastAPI config: FRONTEND_URL
    # Add other origins from your FastAPI BACKEND_CORS_ORIGINS if any
]
# If you want to allow all origins (less secure, for development)
# CORS_ALLOW_ALL_ORIGINS = True

# Django REST framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication', # Use JWT
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated', # Default to requiring authentication
    ),
    # TODO: Configure rate limiting if needed, similar to FastAPI's AUTH_RATE_LIMIT
}

# GitHub OAuth - these will be used by your views/services
GITHUB_CLIENT_ID = "Ov23liI7bdjnUEQpEQwJ"  # Replace with actual value or load from env
GITHUB_CLIENT_SECRET = "6f1e13cf4d4b274465656d12ac174d65187c0272" # Replace with actual value or load from env
GITHUB_CALLBACK_URL = "http://localhost:8000/api/v1/auth/github/callback" # Replace with actual value or load from env
GITHUB_WEBHOOK_SECRET = "lhigjojihgfdtyuiodghj64thjki" # From your FastAPI config
FRONTEND_URL = "http://localhost:5173" # From your FastAPI config
# JWT Settings (if using django-rest-framework-simplejwt)
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30), # From your FastAPI config: ACCESS_TOKEN_EXPIRE_MINUTES
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,

    "ALGORITHM": "HS256", # From your FastAPI config: ALGORITHM
    "SIGNING_KEY": SECRET_KEY, # Uses Django's SECRET_KEY by default
    "VERIFYING_KEY": None,
    "AUDIENCE": None,
    "ISSUER": None,
    "JWK_URL": None,
    "LEEWAY": 0,

    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "USER_AUTHENTICATION_RULE": "rest_framework_simplejwt.authentication.default_user_authentication_rule",

    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
    "TOKEN_USER_CLASS": "rest_framework_simplejwt.models.TokenUser",

    "JTI_CLAIM": "jti",

    # TODO: Add other settings as needed, e.g., for sliding tokens
    # "SLIDING_TOKEN_REFRESH_EXP_CLAIM": "refresh_exp",
    # "SLIDING_TOKEN_LIFETIME": timedelta(minutes=5),
    # "SLIDING_TOKEN_REFRESH_LIFETIME": timedelta(days=1),
}

# LLM Settings
DEFAULT_LLM_MODEL = "CEREBRAS::llama-3.3-70b"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 32768
DEFAULT_MAX_TOOL_CALLS = 7

# LangGraph
LANGGRAPH_API_URL = os.getenv('LANGGRAPH_API_URL', 'http://localhost:8123')
LANGGRAPH_API_KEY = os.getenv('LANGGRAPH_API_KEY', '')
LANGSMITH_API_KEY = os.getenv('LANGSMITH_API_KEY', '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')
# LangGraph Assistant Configuration
LANGGRAPH_REVIEW_ASSISTANT_ID = os.getenv('LANGGRAPH_REVIEW_ASSISTANT_ID', "80c5c4d8-dc67-5ab3-8734-c1e23e87e5ad")
LANGGRAPH_FEEDBACK_ASSISTANT_ID = os.getenv('LANGGRAPH_FEEDBACK_ASSISTANT_ID', "cd380c07-d635-5f75-a268-adf7c2575a03")
AI_USER_ID = os.getenv('AI_USER_ID', 1) # Replace 1 with the actual ID of your AI user after creation

# Celery Configuration
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'django-db'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# It's highly recommended to load sensitive keys and environment-specific settings
# from environment variables rather than hardcoding them.
# For example, using something like python-decouple or os.environ.get()
# SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'your-default-secret-key-if-not-set')
# GITHUB_CLIENT_ID = os.environ.get('GITHUB_CLIENT_ID')
# etc.
