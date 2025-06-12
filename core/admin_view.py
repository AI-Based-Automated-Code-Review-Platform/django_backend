from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser

from .models import (
    User,
    Repository as DBRepository,
    Review as ReviewModel,
    LLMUsage as LLMUsageModel,
)
from .serializers import (
    UserSerializer, AdminUserUpdateSerializer
)
from django.shortcuts import get_object_or_404
import logging
# Create a logger instance
logger = logging.getLogger(__name__)

# Admin endpoints
class AdminStatsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, *args, **kwargs):
        user_count = User.objects.count()
        repo_count = DBRepository.objects.count()
        review_count = ReviewModel.objects.count()
        llm_usage_count = LLMUsageModel.objects.count()
        return Response({
            'users': user_count,
            'repositories': repo_count,
            'reviews': review_count,
            'llm_usages': llm_usage_count,
        })

class AdminUserListView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request, *args, **kwargs):
        users = User.objects.all()
        serializer = UserSerializer(users, many=True)
        return Response(serializer.data)

class AdminUserUpdateView(APIView):
    permission_classes = [IsAuthenticated, IsAdminUser]

    def put(self, request, user_id, *args, **kwargs):
        user = get_object_or_404(User, pk=user_id)
        serializer = AdminUserUpdateSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)
