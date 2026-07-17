"""Client-side access to PledgeManager.default_url (issue #159/#181): the
purchase-edit "Manager" dropdown shows a note when the selected PM has a
shared default link, without a server round-trip per selection. Single
source of truth so the note never drifts from the model's actual data.
"""

import json

from django import template
from django.utils.safestring import mark_safe

from ..models import PledgeManager

register = template.Library()


@register.simple_tag
def pledge_manager_default_urls_json():
    urls = {str(pm.pk): pm.default_url for pm in PledgeManager.objects.all()}
    return mark_safe(json.dumps(urls))
