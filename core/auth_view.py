from django.http import HttpResponseRedirect
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    User
)

from .services import (
    generate_oauth_state,
    validate_oauth_state,
    get_github_oauth_redirect_url,
    exchange_code_for_github_token,
    get_github_user_info,
)
import requests
import urllib.parse
from django.conf import settings
from urllib.parse import urlencode
import logging
# Create a logger instance
logger = logging.getLogger(__name__)


class GitHubLoginView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        # TODO: Implement rate limiting for request.META.get('REMOTE_ADDR')
        state = generate_oauth_state(request)
        redirect_url = get_github_oauth_redirect_url(state)
        return HttpResponseRedirect(redirect_url)

class GitHubLoginRedirectView(APIView):
    permission_classes = [AllowAny]

    async def get(self, request, *args, **kwargs):
        state = generate_oauth_state()  # Assuming this service function exists
        request.session['github_oauth_state'] = state
        # Assuming get_github_oauth_redirect_url service constructs the full URL
        # It would need GITHUB_CLIENT_ID and GITHUB_SCOPES from settings
        try:
            redirect_url = get_github_oauth_redirect_url(state) # This service might need to be async if it does I/O
            return HttpResponseRedirect(redirect_url)
        except Exception as e:
            # Log error e
            error_message = urlencode({"message": "Failed to initiate GitHub login."})
            return HttpResponseRedirect(f"{settings.FRONTEND_URL}/auth/error?{error_message}")

class GitHubCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        code = request.GET.get('code')
        state_from_callback = request.GET.get('state')

        # TODO: Implement rate limiting for request.META.get('REMOTE_ADDR')

        if not code or not state_from_callback:
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote('Missing code or state from GitHub callback.')}"
            return HttpResponseRedirect(error_url)

        if not validate_oauth_state(request, state_from_callback):
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote('Invalid OAuth state.')}"
            return HttpResponseRedirect(error_url)

        try:
            github_token = exchange_code_for_github_token(code)
            if not github_token:
                raise Exception("Failed to retrieve GitHub access token.")

            github_user_info = get_github_user_info(github_token)
            
            # Ensure email is present, if not, try to get it or handle missing email
            user_email = github_user_info.get("email")
            if not user_email:
                # Potentially raise an error or redirect with a message if email is strictly required
                # For now, we'll allow it to be null if not provided by GitHub or primary email not found
                pass 

            user, created = User.objects.update_or_create(
                github_id=str(github_user_info["id"]),
                defaults={
                    'username': github_user_info["login"],
                    'email': user_email,
                    'github_access_token': github_token,
                    # Ensure other required fields for User model are handled if any
                }
            )

            refresh = RefreshToken.for_user(user)
            access_token = str(refresh.access_token)

            # Redirect to frontend with token
            frontend_url = f"{settings.FRONTEND_URL}/auth/callback?token={access_token}&refresh_token={str(refresh)}"
            return HttpResponseRedirect(frontend_url)

        except requests.exceptions.RequestException as e:
            # Log the error: print(e) or use proper logging
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote(f'GitHub API error: {e}')}"
            return HttpResponseRedirect(error_url)
        except Exception as e:
            # Log the error: print(e) or use proper logging
            error_url = f"{settings.FRONTEND_URL}/auth/error?message={urllib.parse.quote(f'An unexpected error occurred: {e}')}"
            return HttpResponseRedirect(error_url)

class GitHubExchangeAuthTokenView(APIView):
    permission_classes = [AllowAny]

    async def post(self, request, *args, **kwargs):
        code = request.data.get('code')

        if not code:
            return Response({"detail": "Authorization code is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            github_token_data = await exchange_code_for_github_token(code)
            if isinstance(github_token_data, str):
                github_access_token = github_token_data
            elif isinstance(github_token_data, dict) and 'access_token' in github_token_data:
                github_access_token = github_token_data['access_token']
            else:
                raise ValueError("Invalid token data from GitHub service")
                
            github_user_info = await get_github_user_info(github_access_token)

            user, created = await User.objects.aget_or_create(
                github_id=str(github_user_info["id"]),
                defaults={
                    "username": github_user_info["login"],
                    "email": github_user_info.get("email"),
                    "github_access_token": github_access_token,
                }
            )

            if not created:
                user.username = github_user_info["login"]
                user.email = github_user_info.get("email")
                user.github_access_token = github_access_token
                await user.asave()
            
            refresh = RefreshToken.for_user(user)
            
            return Response({
                "access_token": str(refresh.access_token),
                "refresh_token": str(refresh),
                "token_type": "bearer"
            }, status=status.HTTP_200_OK)

        except Exception as e:
            # Log error e
            return Response({"detail": f"GitHub authentication failed: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)