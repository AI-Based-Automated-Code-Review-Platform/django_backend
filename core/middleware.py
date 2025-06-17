from channels.middleware import BaseMiddleware
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import UntypedToken
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from urllib.parse import parse_qs, unquote
import logging

logger = logging.getLogger(__name__)
User = get_user_model()

@database_sync_to_async
def get_user_from_jwt_token(token):
    """
    Get user from JWT token (synchronous function wrapped for async use)
    """
    try:
        # Decode the token if it's URL encoded
        decoded_token = unquote(token)
        logger.info(f"🔍 JWT Middleware - Token validation starting (length: {len(decoded_token)})")
        
        # Validate the token
        UntypedToken(decoded_token)
        jwt_auth = JWTAuthentication()
        validated_token = jwt_auth.get_validated_token(decoded_token)
        user = jwt_auth.get_user(validated_token)
        
        logger.info(f"✅ JWT Middleware - Successfully authenticated user: {user.username} (ID: {user.github_id})")
        return user
        
    except (InvalidToken, TokenError) as e:
        logger.warning(f"❌ JWT Middleware - Token validation failed: {e}")
        return AnonymousUser()
    except Exception as e:
        logger.error(f"❌ JWT Middleware - Unexpected error: {e}")
        return AnonymousUser()

class JWTWebSocketMiddleware(BaseMiddleware):
    """
    Custom JWT authentication middleware for WebSocket connections
    Extracts JWT token from query parameters and authenticates the user
    """
    
    async def __call__(self, scope, receive, send):
        # Only process WebSocket connections
        if scope['type'] != 'websocket':
            return await super().__call__(scope, receive, send)
        
        # Extract token from query string
        query_string = scope.get('query_string', b'').decode()
        logger.info(f"🔍 JWT Middleware - Processing WebSocket connection with query: {query_string[:100]}...")
        
        token = None
        if query_string:
            # Parse query parameters
            query_params = parse_qs(query_string)
            token_list = query_params.get('token', [])
            
            if token_list:
                token = token_list[0]
                logger.info(f"✅ JWT Middleware - Extracted token from query params")
            else:
                # Fallback to simple parsing
                for param in query_string.split('&'):
                    if param.startswith('token='):
                        token = param.split('=', 1)[1]
                        logger.info(f"✅ JWT Middleware - Extracted token using fallback method")
                        break
        
        # Authenticate user with token
        if token:
            user = await get_user_from_jwt_token(token)
            scope['user'] = user
            logger.info(f"🔐 JWT Middleware - Set user in scope: {user} (authenticated: {user.is_authenticated})")
        else:
            scope['user'] = AnonymousUser()
            logger.warning(f"❌ JWT Middleware - No token found, setting anonymous user")
        
        return await super().__call__(scope, receive, send)

def JWTAuthMiddlewareStack(inner):
    """
    Middleware stack that includes JWT authentication
    Use this instead of AuthMiddlewareStack for JWT-based WebSocket authentication
    """
    return JWTWebSocketMiddleware(inner) 