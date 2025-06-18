from django.test import TestCase
from core.tasks import review_tasks

class TaskTests(TestCase):
    def test_calculate_cost_default(self):
        usage = {'input_tokens': 100, 'output_tokens': 200}
        cost = review_tasks.calculate_cost(usage, 'default')
        self.assertAlmostEqual(cost, 0.005, places=6)

    def test_calculate_cost_gpt4(self):
        usage = {'input_tokens': 100, 'output_tokens': 200}
        cost = review_tasks.calculate_cost(usage, 'gpt-4')
        self.assertAlmostEqual(cost, 0.015, places=6)

    def test_calculate_cost_llama(self):
        usage = {'input_tokens': 100, 'output_tokens': 200}
        cost = review_tasks.calculate_cost(usage, 'cerebras::llama-3.3-70b')
        self.assertAlmostEqual(cost, 0.00096, places=6) 