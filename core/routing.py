from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/reviews/(?P<review_id>\w+)/$', consumers.ReviewConsumer.as_asgi()),
    re_path(r'ws/user/(?P<user_id>\w+)/$', consumers.UserConsumer.as_asgi()),
] 