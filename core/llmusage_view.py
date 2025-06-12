from rest_framework.response import Response
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from .models import (
    LLMUsage as LLMUsageModel,
)
from .serializers import (
    LLMUsageSerializer
)

from django.db.models import Q, Sum, Count
import logging
# Create a logger instance
logger = logging.getLogger(__name__)

class LLMUsageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = LLMUsageSerializer
    permission_classes = [IsAuthenticated]

    def _get_base_llm_usage_queryset(self):
        """
        Helper method to get the base QuerySet of LLMUsageModel instances
        based on user permissions.
        """
        if self.request.user.is_staff:
            return LLMUsageModel.objects.all()
        else:
            return LLMUsageModel.objects.filter(
                Q(review__repository__owner=self.request.user) |
                Q(review__repository__collaborators__user=self.request.user)
            ).distinct()

    def get_queryset(self):
        """
        This method is intended to return a summary dictionary.
        It's called by the overridden 'list' and 'summary' actions.
        Standard DRF ModelViewSet 'retrieve' action will break if it relies on this
        method returning a QuerySet of model instances.
        """
        base_queryset = self._get_base_llm_usage_queryset()

        total_tokens = base_queryset.aggregate(
            total_input=Sum('input_tokens', default=0),
            total_output=Sum('output_tokens', default=0),
            total_cost=Sum('cost', default=0.0)
        )
        
        usage_by_model = list(base_queryset.values('llm_model').annotate(
            total_input=Sum('input_tokens', default=0),
            total_output=Sum('output_tokens', default=0),
            total_cost=Sum('cost', default=0.0),
            count=Count('id')
        ).order_by('llm_model'))

        # Ensure all parts of total_tokens are not None if the base_queryset is empty
        total_usage_cleaned = {
            'total_input': total_tokens['total_input'] or 0,
            'total_output': total_tokens['total_output'] or 0,
            'total_cost': total_tokens['total_cost'] or 0.0,
        }

        return {
            'total_usage': total_usage_cleaned,
            'usage_by_model': usage_by_model
        }


    def list(self, request, *args, **kwargs):
        """
        Overrides the default list action to return the summary data.
        """
        summary_data = self.get_queryset() # This now calls the method that returns the dictionary
        return Response(summary_data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """
        Custom action to return the summary. This is consistent with the list view.
        """
        summary_data = self.get_queryset() # Calls the method that returns the dictionary
        return Response(summary_data)