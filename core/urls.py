from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .auth_view import GitHubLoginView, GitHubCallbackView, GitHubExchangeAuthTokenView, GitHubLoginRedirectView
from .admin_view import AdminStatsView, AdminUserListView, AdminUserUpdateView
from .webhook_view import github_webhook
from .user_view import CurrentUserView, UserDashboardStatsView, UserRepositoriesView, UserOrganizationsView
from .repository_view import RepositoryViewSet
from .pr_view import PullRequestViewSet
from .commit_view import CommitViewSet
from .review_view import ReviewViewSet
from .llmusage_view import LLMUsageViewSet
from .thread_view import ThreadViewSet
router = DefaultRouter()

router.register(r'repositories', RepositoryViewSet, basename='repository')
router.register(r'pull-requests', PullRequestViewSet, basename='pullrequest')
router.register(r'commits', CommitViewSet, basename='commit')
router.register(r'reviews', ReviewViewSet, basename='review')
router.register(r'llm-usage', LLMUsageViewSet, basename='llmusage')
router.register(r'threads', ThreadViewSet, basename='thread')
# router.register(r'comments', views.CommentViewSet, basename='comment')
# router.register(r'review-feedback', views.ReviewFeedbackViewSet, basename='reviewfeedback')
# Optionally: router.register(r'admin', views.AdminViewSet, basename='admin')

urlpatterns = [
    # Auth endpoints
    path('auth/github/login/', GitHubLoginView.as_view(), name='github_login'),
    path('auth/github/callback/', GitHubCallbackView.as_view(), name='github_callback'),
    # path('auth/github/', views.GitHubDirectAuthView.as_view(), name='github_direct_auth'), # This was commented out, ensure it's needed or remove
    path('auth/github/exchange/', GitHubExchangeAuthTokenView.as_view(), name='github_exchange_token'), # Added for explicit token exchange

    # User endpoints are now part of UserViewSet, accessed via router
    path('user/', CurrentUserView.as_view(), name='current_user'), # Replaced by users/me
    path('user/repos/', UserRepositoriesView.as_view(), name='user_repositories'), # Replaced by users/repos
    path('user/organizations/', UserOrganizationsView.as_view(), name='user_organizations'), # Replaced by users/organizations
    path('user/dashboard-stats/', UserDashboardStatsView.as_view(), name='user_dashboard_stats'), # Replaced by users/dashboard-stats

    # Repository, pr, commit, reviews, llm-usage and threads endpoints (router)
    path('', include(router.urls)),

    path('webhook/github/', github_webhook, name='github-webhook'),

    # Admin endpoints
    path('admin/stats/', AdminStatsView.as_view(), name='admin_stats'),
    path('admin/users/', AdminUserListView.as_view(), name='admin_list_users'),
    path('admin/users/<int:user_id>/', AdminUserUpdateView.as_view(), name='admin_update_user'),
]