from django.db import models

from STORA.accounts.models import Employee


class AIReport(models.Model):
    """A saved natural-language report ("AI mode"). Stores the ORIGINAL
    prompt plus the SQL Claude generated for it last time -- opening the
    report re-runs that stored SQL against the live 'readonly' DB
    connection every time (see AIReportDetailView), so the numbers shown
    are always current, never a frozen snapshot. The prompt itself is
    never re-sent to Claude just to view a report -- only "Regenerate"
    does that (re-derives the SQL and the narrative answer from scratch,
    e.g. after the prompt's intent needs reinterpreting or the schema
    changed), which is why `generated_sql`/`last_answer` are separate,
    explicitly-updated fields rather than always-fresh computed ones.
    """

    prompt = models.TextField(verbose_name='Question')
    generated_sql = models.TextField(blank=True)
    last_answer = models.TextField(blank=True, verbose_name='Last narrative answer')
    created_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, related_name='ai_reports')
    created_at = models.DateTimeField(auto_now_add=True)
    last_regenerated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'AI Report'
        verbose_name_plural = 'AI Reports'
        ordering = ['-created_at']

    def __str__(self):
        return self.prompt[:60]
