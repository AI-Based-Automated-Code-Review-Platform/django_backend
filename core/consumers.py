import json
import logging
from urllib.parse import parse_qs, unquote
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import UntypedToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.contrib.auth.models import AnonymousUser
from .models import Review as ReviewModel
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)
User = get_user_model()

@database_sync_to_async
def get_user_from_token(token):
    try:
        # Decode the token if it's URL encoded
        decoded_token = unquote(token)
        logger.info(f"🔍 Token validation - Original length: {len(token)}, Decoded length: {len(decoded_token)}")
        logger.info(f"🔍 Attempting to validate token: {decoded_token[:50]}...")
        
        UntypedToken(decoded_token)
        jwt_auth = JWTAuthentication()
        validated_token = jwt_auth.get_validated_token(decoded_token)
        user = jwt_auth.get_user(validated_token)
        logger.info(f"✅ Successfully authenticated user: {user.username} (ID: {user.github_id})")
        return user
    except (InvalidToken, TokenError) as e:
        logger.error(f"❌ JWT validation failed: {e}")
        logger.error(f"   Token (first 100 chars): {token[:100]}")
        return AnonymousUser()
    except Exception as e:
        logger.error(f"❌ Unexpected error during token validation: {e}")
        logger.error(f"   Token (first 100 chars): {token[:100]}")
        return AnonymousUser()

class ReviewConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.review_id = self.scope['url_route']['kwargs']['review_id']
        self.review_group_name = f'review_{self.review_id}'
        
        # Get user from scope (set by JWT middleware)
        user = self.scope.get('user')
        
        logger.info(f"🔌 WebSocket connection attempt for review {self.review_id}")
        logger.info(f"🔍 User from scope: {user} (authenticated: {user.is_authenticated if user else 'None'})")

        # Check if user is authenticated
        if user and user.is_authenticated:
            # Join review group
            await self.channel_layer.group_add(
                self.review_group_name,
                self.channel_name
            )
            await self.accept()
            logger.info(f"✅ WebSocket connected for review {self.review_id} by user {user.username}")
            return
        
        # Reject connection if not authenticated
        await self.close(code=4003)  # 4003 = Forbidden
        logger.warning(f"❌ WebSocket connection rejected for review {self.review_id} - not authenticated")

    async def extract_token_from_query(self):
        """Extract token from query string with proper URL decoding"""
        query_string = self.scope.get('query_string', b'').decode()
        logger.info(f"🔍 Raw query string: '{query_string}'")
        
        if not query_string:
            logger.warning("❌ No query string found")
            return None
            
        # Parse query parameters
        query_params = parse_qs(query_string)
        logger.info(f"🔍 Parsed query params: {query_params}")
        token_list = query_params.get('token', [])
        
        if token_list:
            token = token_list[0]  # Get first token value
            logger.info(f"✅ Extracted token from parse_qs (first 50 chars): {token[:50]}...")
            return token
        
        # Fallback to simple parsing
        for param in query_string.split('&'):
            if param.startswith('token='):
                token = param.split('=', 1)[1]
                logger.info(f"✅ Fallback extracted token (first 50 chars): {token[:50]}...")
                return token
                
        logger.warning(f"❌ No token found in query string: {query_string}")
        return None

    async def disconnect(self, close_code):
        # Leave review group
        await self.channel_layer.group_discard(
            self.review_group_name,
            self.channel_name
        )
        logger.info(f"WebSocket disconnected for review {self.review_id}")

    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            message_type = text_data_json.get('type')
            
            if message_type == 'ping':
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': text_data_json.get('timestamp')
                }))
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {text_data}")

    # Receive message from review group
    async def review_update(self, event):
        # Send message to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'review_update',
            'status': event['status'],
            'progress': event.get('progress'),
            'message': event.get('message'),
            'data': event.get('data')
        }))

    async def review_completed(self, event):
        # Send completion message to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'review_completed',
            'review_data': event['review_data'],
            'token_usage': event.get('token_usage'),
            'thread_id': event.get('thread_id')
        }))

    async def review_error(self, event):
        # Send error message to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'review_error',
            'error': event['error'],
            'message': event.get('message')
        }))

class UserConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user_id = self.scope['url_route']['kwargs']['user_id']
        self.user_group_name = f'user_{self.user_id}'
        
        # Get user from scope (set by JWT middleware)
        user = self.scope.get('user')
        
        logger.info(f"🔌 WebSocket connection attempt for user {self.user_id}")
        logger.info(f"🔍 User from scope: {user} (authenticated: {user.is_authenticated if user else 'None'})")
        
        # Check if user is authenticated and matches the requested user ID
        if user and user.is_authenticated and str(user.github_id) == self.user_id:
            # Join user group
            await self.channel_layer.group_add(
                self.user_group_name,
                self.channel_name
            )
            await self.accept()
            logger.info(f"✅ WebSocket connected for user {self.user_id} ({user.username})")
            return
        
        # Reject connection if not authenticated or user ID mismatch
        await self.close(code=4003)  # 4003 = Forbidden
        if user and user.is_authenticated:
            logger.warning(f"❌ WebSocket connection rejected for user {self.user_id} - user ID mismatch (authenticated as {user.github_id})")
        else:
            logger.warning(f"❌ WebSocket connection rejected for user {self.user_id} - not authenticated")

    async def extract_token_from_query(self):
        """Extract token from query string with proper URL decoding"""
        query_string = self.scope.get('query_string', b'').decode()
        logger.info(f"🔍 Raw query string: '{query_string}'")
        
        if not query_string:
            logger.warning("❌ No query string found")
            return None
            
        # Parse query parameters
        query_params = parse_qs(query_string)
        logger.info(f"🔍 Parsed query params: {query_params}")
        token_list = query_params.get('token', [])
        
        if token_list:
            token = token_list[0]  # Get first token value
            logger.info(f"✅ Extracted token from parse_qs (first 50 chars): {token[:50]}...")
            return token
        
        # Fallback to simple parsing
        for param in query_string.split('&'):
            if param.startswith('token='):
                token = param.split('=', 1)[1]
                logger.info(f"✅ Fallback extracted token (first 50 chars): {token[:50]}...")
                return token
                
        logger.warning(f"❌ No token found in query string: {query_string}")
        return None

    async def disconnect(self, close_code):
        # Leave user group
        await self.channel_layer.group_discard(
            self.user_group_name,
            self.channel_name
        )
        logger.info(f"WebSocket disconnected for user {self.user_id}")

    async def receive(self, text_data):
        try:
            text_data_json = json.loads(text_data)
            message_type = text_data_json.get('type')
            
            if message_type == 'ping':
                await self.send(text_data=json.dumps({
                    'type': 'pong',
                    'timestamp': text_data_json.get('timestamp')
                }))
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON received: {text_data}")

    # Receive message from user group
    async def user_notification(self, event):
        # Send notification to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'notification',
            'title': event['title'],
            'message': event['message'],
            'data': event.get('data')
        }))

    async def review_status_update(self, event):
        # Send review status update to WebSocket
        await self.send(text_data=json.dumps({
            'type': 'review_status_update',
            'review_id': event['review_id'],
            'status': event['status'],
            'progress': event.get('progress')
        })) 