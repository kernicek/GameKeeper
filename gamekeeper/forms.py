"""Django forms for the two pages unified under issue #136 (purchase_edit's
campaign + waves, copy_edit's details + curation) — one Save each instead of
several independent whole-form POSTs that could silently drop each other's
edits. Every other view in this app hand-parses request.POST directly; this
is deliberately scoped to just these two pages, not a house-wide migration.
"""

from decimal import Decimal

from django import forms
from django.core.validators import URLValidator

from .models import Copy, Location, PledgePlan, PledgePlanBundle, PledgePlanItem, Purchase, Wave

# Stored URLs render as plain hrefs, so anything non-http(s) is refused
# outright (a browser's type=url input doesn't constrain the scheme).
HTTP_URL_VALIDATORS = [URLValidator(schemes=["http", "https"])]

# purchase_edit.html lays out the campaign card, the wave sub-cards and the
# Save button across a wider chunk of the page than one literal <form> can
# cleanly wrap (waves nest their own "Add item" <form>, and HTML forms can't
# nest). The HTML5 form="" attribute lets every field submit with the one
# <form id="purchase-edit-form"> regardless of where it sits in the DOM.
PURCHASE_EDIT_FORM_ID = "purchase-edit-form"


def _bind_to_purchase_edit_form(fields):
    for field in fields:
        field.widget.attrs["form"] = PURCHASE_EDIT_FORM_ID


def _clean_excitement(value):
    if value is None:
        return None
    if not 0 <= value <= 10:
        raise forms.ValidationError("Excitement is 0–10.")
    return value.quantize(Decimal("0.1"))


class PurchaseForm(forms.ModelForm):
    # Explicitly-declared fields (needed for the http(s)-only validator)
    # don't pick up Meta.widgets, so the form-control class is set here.
    campaign_url = forms.URLField(
        required=False, validators=HTTP_URL_VALIDATORS,
        widget=forms.URLInput(attrs={"class": "form-control"}))
    pledge_manager_url = forms.URLField(
        required=False, validators=HTTP_URL_VALIDATORS,
        widget=forms.URLInput(attrs={"class": "form-control"}))

    class Meta:
        model = Purchase
        fields = [
            "name", "platform", "status", "campaign_url", "campaign_end_date",
            "ordered_date", "pledge_manager", "pledge_manager_url",
            "pledge_manager_status", "pledge_manager_close_date",
            "excitement", "excitement_note", "comments",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "platform": forms.Select(attrs={"class": "form-select"}),
            "status": forms.Select(attrs={"class": "form-select"}),
            "campaign_end_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "ordered_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "pledge_manager": forms.Select(attrs={"class": "form-select"}),
            "pledge_manager_status": forms.Select(attrs={"class": "form-select"}),
            "pledge_manager_close_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "excitement": forms.NumberInput(attrs={
                "class": "form-control", "min": 0, "max": 10, "step": "0.1"}),
            "excitement_note": forms.TextInput(attrs={"class": "form-control"}),
            "comments": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, owner, bind_to_edit_form=False, **kwargs):
        # The (owner, name) uniqueness check needs the owner even though
        # it isn't a form field itself (the view sets it on the instance).
        # purchase_add still wraps its own fields in a plain <form>, so only
        # purchase_edit (see PURCHASE_EDIT_FORM_ID above) asks for the rebind.
        self.owner = owner
        super().__init__(*args, **kwargs)
        if bind_to_edit_form:
            _bind_to_purchase_edit_form(self.fields.values())

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        existing = Purchase.objects.filter(owner=self.owner, name=name)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(
                "You already have a purchase with that name.")
        return name

    def clean_excitement(self):
        return _clean_excitement(self.cleaned_data.get("excitement"))


class WaveForm(forms.ModelForm):
    tracking_url = forms.URLField(
        required=False, validators=HTTP_URL_VALIDATORS,
        widget=forms.URLInput(attrs={"class": "form-control"}))

    class Meta:
        model = Wave
        fields = [
            "status", "delivery_type", "original_eta", "expected_arrival",
            "arrived_date", "address", "tracking_url",
        ]
        widgets = {
            "status": forms.Select(attrs={"class": "form-select"}),
            "delivery_type": forms.Select(attrs={"class": "form-select"}),
            "original_eta": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "expected_arrival": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "arrived_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "address": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _bind_to_purchase_edit_form(self.fields.values())


class WaveFormSetBase(forms.BaseInlineFormSet):
    def add_fields(self, form, index):
        # Adds the hidden pk field and (can_delete=True) the DELETE
        # checkbox — both need the same form="" rebind as everything else,
        # since they render outside <form id="purchase-edit-form"> too.
        super().add_fields(form, index)
        _bind_to_purchase_edit_form(form.fields.values())

    def clean(self):
        # Mirrors the old wave_delete guard: waves with items stay put —
        # move or delete the products first. A per-form clean() can't do
        # this — Django's formset skips field validation entirely for
        # forms marked for deletion, so the guard has to live here instead
        # (formset-level errors aren't skipped that way).
        super().clean()
        for form in self.forms:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE") and form.instance.products.exists():
                raise forms.ValidationError(
                    f"Wave {form.instance.number} still has items on it.")


WaveFormSet = forms.inlineformset_factory(
    Purchase, Wave, form=WaveForm, formset=WaveFormSetBase, extra=0, can_delete=True,
)


class CopyForm(forms.ModelForm):
    class Meta:
        model = Copy
        fields = [
            "edition", "acquired_date", "location", "location_note",
            "insert_3d", "card_dividers", "accessories_3d", "other_accessories",
            "upgrades_note", "notes",
            "excitement", "keep_status", "immune", "why_might_leave",
        ]
        widgets = {
            "edition": forms.Select(attrs={"class": "form-select"}),
            "acquired_date": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}),
            "location": forms.Select(attrs={"class": "form-select"}),
            "location_note": forms.TextInput(attrs={"class": "form-control"}),
            "insert_3d": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "card_dividers": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "accessories_3d": forms.Select(attrs={"class": "form-select form-select-sm"}),
            "other_accessories": forms.Select(
                attrs={"class": "form-select form-select-sm"}),
            "upgrades_note": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "excitement": forms.NumberInput(attrs={
                "class": "form-control", "min": 0, "max": 10, "step": "0.5"}),
            "keep_status": forms.Select(attrs={"class": "form-select"}),
            "immune": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "why_might_leave": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, owner, game, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)
        # Editions another of the user's *owned* copies sits on are not
        # offered — unique per (owner, edition), but only among non-borrowed
        # copies (issue #43): a borrowed-in copy may duplicate an edition the
        # user already owns (or has borrowed from someone else), and a
        # borrowed-in copy itself is never unique-constrained.
        taken = set() if self.instance.is_borrowed_in else set(
            Copy.objects.filter(owner=owner, edition__game=game, is_borrowed_in=False)
            .exclude(pk=self.instance.pk).values_list("edition_id", flat=True)
        )
        self.fields["edition"].queryset = game.editions.exclude(pk__in=taken)
        membership = getattr(owner, "membership", None)
        self.fields["location"].queryset = (
            membership.group.locations.order_by("name") if membership
            else Location.objects.none()
        )
        self.fields["location"].empty_label = "—"

    def clean_edition(self):
        edition = self.cleaned_data["edition"]
        if (not self.instance.is_borrowed_in
                and edition.pk != self.instance.edition_id
                and Copy.objects.filter(
                    owner=self.owner, edition=edition, is_borrowed_in=False).exists()):
            raise forms.ValidationError("You already have a copy of this edition.")
        return edition

    def clean_excitement(self):
        return _clean_excitement(self.cleaned_data.get("excitement"))


class PledgePlanForm(forms.ModelForm):
    class Meta:
        model = PledgePlan
        fields = ["currency", "vat_rate", "czk_rate"]
        widgets = {
            "currency": forms.TextInput(attrs={"class": "form-control"}),
            "vat_rate": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"}),
            "czk_rate": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.0001"}),
        }

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().upper()


class PledgePlanItemForm(forms.ModelForm):
    class Meta:
        model = PledgePlanItem
        fields = ["name", "category", "want_priority", "price", "notes"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "want_priority": forms.Select(attrs={"class": "form-select"}),
            "price": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"}),
            "notes": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, plan, **kwargs):
        self.plan = plan
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        existing = PledgePlanItem.objects.filter(plan=self.plan, name=name)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("This plan already has an item with that name.")
        return name


class PledgePlanBundleForm(forms.ModelForm):
    class Meta:
        model = PledgePlanBundle
        fields = ["name", "price", "shipping_cost"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "price": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"}),
            "shipping_cost": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"}),
        }

    def __init__(self, *args, plan, **kwargs):
        self.plan = plan
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        existing = PledgePlanBundle.objects.filter(plan=self.plan, name=name)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError("This plan already has a bundle with that name.")
        return name
