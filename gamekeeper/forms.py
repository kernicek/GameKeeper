"""Django forms for the app's edit pages. Started under issue #136
(purchase_edit's campaign + waves, copy_edit's details + curation) — one
Save each instead of several independent whole-form POSTs that could
silently drop each other's edits — then extended to the remaining edit
pages under issue #28 for consistent validation and error rendering.
"""

import io
from decimal import Decimal

from PIL import Image

from django import forms
from django.core.validators import URLValidator
from django.db.models import Q

from .models import (
    Copy, Edition, Family, Game, Location, PledgePlan, PledgePlanBundle,
    PledgePlanItem, Product, Purchase, Series, Wave,
)

# Stored URLs render as plain hrefs, so anything non-http(s) is refused
# outright (a browser's type=url input doesn't constrain the scheme).
HTTP_URL_VALIDATORS = [URLValidator(schemes=["http", "https"])]

COVER_MAX_BYTES = 20 * 1024 * 1024
# Web-safe formats only — a browser has to render the file straight from
# media/covers/. Pillow names the format; anything else is rejected.
COVER_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}


def _validate_cover_image(data):
    """Validate cover image bytes for browser-renderable formats. Returns
    (error, image) — error is "" on success; the (verified) Pillow image
    still answers .format and .size."""
    if len(data) > COVER_MAX_BYTES:
        return "Image is too large (20 MB max).", None
    try:
        image = Image.open(io.BytesIO(data))
        image.verify()
    except Exception:
        return "That does not look like an image file.", None
    if image.format not in COVER_EXTENSIONS:
        return (f"{image.format} images will not render in browsers — "
                "use JPEG, PNG, GIF or WebP."), None
    return "", image

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
        fields = ["name", "category", "want_priority", "price", "notes", "exclusive"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "want_priority": forms.Select(attrs={"class": "form-select"}),
            "price": forms.NumberInput(
                attrs={"class": "form-control", "min": 0, "step": "0.01"}),
            "notes": forms.TextInput(attrs={"class": "form-control"}),
            "exclusive": forms.CheckboxInput(attrs={"class": "form-check-input"}),
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


class ProductForm(forms.ModelForm):
    # Explicitly-declared (needed for the http(s)-only validator — the
    # model's plain URLField doesn't restrict the scheme).
    bgg_url = forms.URLField(
        max_length=500, required=False, validators=HTTP_URL_VALIDATORS,
        widget=forms.URLInput(attrs={"class": "form-control", "placeholder": "https://…"}))
    drive_url = forms.URLField(
        max_length=1000, required=False, validators=HTTP_URL_VALIDATORS,
        widget=forms.URLInput(attrs={"class": "form-control", "placeholder": "https://…"}))

    class Meta:
        model = Product
        fields = [
            "name", "kind", "game", "edition", "contains_cards",
            "needs_sleeves", "miniatures_count", "fits_sleeved_note",
            "insert_3d_note", "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "kind": forms.Select(attrs={"class": "form-select"}),
            "game": forms.Select(attrs={"class": "form-select"}),
            "edition": forms.Select(attrs={"class": "form-select"}),
            "contains_cards": forms.Select(
                attrs={"class": "form-select form-select-sm"}),
            "needs_sleeves": forms.Select(
                attrs={"class": "form-select form-select-sm"}),
            "miniatures_count": forms.NumberInput(
                attrs={"class": "form-control form-control-sm", "min": 0}),
            "fits_sleeved_note": forms.TextInput(
                attrs={"class": "form-control form-control-sm"}),
            "insert_3d_note": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "moves to the copy on arrival"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["game"].queryset = Game.objects.order_by("name")
        self.fields["game"].empty_label = "— not a modelled game —"
        # Not narrowed to the posted game's editions here — the legal set
        # depends on the *posted* game, resolved in clean() below, same as
        # the old hand-parsed view. A stale/foreign edition pk is a valid
        # Edition row, so it passes field validation and is cleared in
        # clean(); only a nonexistent pk now surfaces as "Select a valid
        # choice" (a minor tightening over the old silent clear-on-any).
        self.fields["edition"].queryset = Edition.objects.all()
        self.fields["edition"].empty_label = "—"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        existing = self.instance.wave.products.filter(name=name)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(
                "This wave already has an item with that name.")
        return name

    def clean(self):
        cleaned = super().clean()
        game, edition = cleaned.get("game"), cleaned.get("edition")
        # The edition select trails the posted game: a mismatch clears it
        # rather than rejecting the submission (changing the game means
        # save, then pick — issue #5).
        if edition is not None and (game is None or edition.game_id != game.pk):
            cleaned["edition"] = None
        return cleaned


class EditionForm(forms.ModelForm):
    """Shared by edition_add and edition_edit (issue #53/#28). Blank name is
    legal — it reads as "default edition" everywhere. Switching to default
    while the game already has one needs a ride-along confirmation (the
    template's confirm modal vouches for it); on confirm, the view demotes
    and renames the old default in the same transaction as this save."""

    confirm_default_switch = forms.BooleanField(required=False)
    old_default_name = forms.CharField(
        required=False, max_length=200,
        widget=forms.TextInput(attrs={"class": "form-control"}))

    class Meta:
        model = Edition
        fields = [
            "name", "components_language", "size_category", "bgg_version_id",
            "num_boxes", "box_length_mm", "box_width_mm", "box_height_mm",
            "is_pnp", "is_default",
        ]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control", "placeholder": "e.g. Kickstarter Edition"}),
            "components_language": forms.Select(attrs={"class": "form-select"}),
            "size_category": forms.Select(attrs={"class": "form-select"}),
            "bgg_version_id": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "num_boxes": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "box_length_mm": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "box_width_mm": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "box_height_mm": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "is_pnp": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edition-pnp"}),
            "is_default": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edition-default"}),
        }

    def __init__(self, *args, game, **kwargs):
        self.game = game
        self.old_default = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("is_default"):
            self.old_default = (
                self.game.editions.filter(is_default=True)
                .exclude(pk=self.instance.pk).first()
            )
            if self.old_default is not None and not cleaned.get("confirm_default_switch"):
                raise forms.ValidationError(
                    "The game already has a default edition — confirm the switch.")
        return cleaned


class FamilyForm(forms.ModelForm):
    """Shared by family_add and family_edit (issue #42/#28): every base game
    is a candidate (loose M2M, no claimed-elsewhere restriction). The plain
    cover upload only applies on create — once the family exists, the htmx
    cover editor owns it, so the field is dropped in __init__ when editing."""

    members = forms.ModelMultipleChoiceField(
        queryset=Game.objects.filter(type=Game.Type.BASE), required=False)
    cover = forms.ImageField(required=False, widget=forms.FileInput(
        attrs={"class": "form-control", "accept": "image/*", "id": "family-cover"}))

    class Meta:
        model = Family
        fields = ["name", "bgg_family_id", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "id": "family-name"}),
            "bgg_family_id": forms.NumberInput(attrs={
                "class": "form-control", "min": 1, "placeholder": "optional",
                "id": "family-bgg-id"}),
            "note": forms.Textarea(attrs={
                "class": "form-control", "rows": 2, "id": "family-note"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial.setdefault(
                "members", list(self.instance.members.values_list("pk", flat=True)))
            del self.fields["cover"]

    def clean_cover(self):
        upload = self.cleaned_data.get("cover")
        if upload:
            data = upload.read()
            error, _ = _validate_cover_image(data)
            if error:
                raise forms.ValidationError(error)
            self.cover_data = data
        return upload


class GameForm(forms.ModelForm):
    """The game_edit details form (issue #28). series/families are real
    Game fields (unlike Series.primary_game — Game has no model-level
    clean(), so there's no full_clean() ordering hazard here) and are only
    offered for base games; the expansion-only overrides are the mirror
    image. alternate_names isn't a Game field at all — it maps to the
    child AlternateName rows, replaced wholesale by the view after save."""

    alternate_names = forms.CharField(required=False, widget=forms.Textarea(
        attrs={"class": "form-control", "id": "edit-alt-names", "rows": 3}))

    class Meta:
        model = Game
        fields = [
            "name", "language_dependency", "language_dependency_note",
            "companion_app", "is_campaign", "is_legacy", "has_scenarios",
            "is_one_off", "has_app_version", "soundtrack_ambience",
            "soundtrack_timer", "player_conflict", "player_conflict_note",
            "players_min_override", "players_max_override",
            "playtime_delta_override", "series", "families",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "id": "edit-name"}),
            "language_dependency": forms.Select(attrs={
                "class": "form-select", "id": "edit-language"}),
            "language_dependency_note": forms.TextInput(attrs={
                "class": "form-control", "id": "edit-language-note"}),
            "companion_app": forms.Select(attrs={
                "class": "form-select", "id": "edit-companion"}),
            "is_campaign": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-campaign"}),
            "is_legacy": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-legacy"}),
            "has_scenarios": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-scenarios"}),
            "is_one_off": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-one-off"}),
            "has_app_version": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-app-version"}),
            "soundtrack_ambience": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-ambience"}),
            "soundtrack_timer": forms.CheckboxInput(attrs={
                "class": "form-check-input", "id": "edit-timer"}),
            "player_conflict": forms.NumberInput(attrs={
                "class": "form-control", "id": "edit-conflict", "min": 0, "max": 3}),
            "player_conflict_note": forms.TextInput(attrs={
                "class": "form-control", "id": "edit-conflict-note"}),
            "players_min_override": forms.NumberInput(attrs={
                "class": "form-control", "id": "edit-min-override", "min": 1}),
            "players_max_override": forms.NumberInput(attrs={
                "class": "form-control", "id": "edit-max-override", "min": 1}),
            "playtime_delta_override": forms.NumberInput(attrs={
                "class": "form-control", "id": "edit-playtime-delta"}),
            "series": forms.Select(attrs={"class": "form-select", "id": "edit-series"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        game = self.instance
        self.fields["series"].queryset = Series.objects.all()
        self.fields["series"].empty_label = "—"
        self.initial.setdefault("alternate_names", "\n".join(
            game.alternate_names.values_list("name", flat=True)) if game.pk else "")
        # Issue #78: Series/Family membership and the expansion-only stat
        # overrides are mutually exclusive by game type — mirrors
        # _series_edit_context/_family_edit_context's own base-game scoping.
        if game.type == Game.Type.BASE:
            for name in ("players_min_override", "players_max_override",
                         "playtime_delta_override"):
                del self.fields[name]
        else:
            for name in ("series", "families"):
                del self.fields[name]

    def clean_series(self):
        series = self.cleaned_data.get("series")
        game = self.instance
        # A series' primary_game must stay one of its members (enforced from
        # the other side by SeriesForm) — block orphaning it from here.
        if game.series_id and (series is None or series.pk != game.series_id) \
                and Series.objects.filter(
                    pk=game.series_id, primary_game_id=game.pk).exists():
            raise forms.ValidationError(
                "This game is its series' primary game — change the "
                "series' primary game first.")
        return series

    def clean_player_conflict(self):
        value = self.cleaned_data.get("player_conflict")
        if value is not None and not 0 <= value <= 3:
            raise forms.ValidationError("Player conflict is 0–3.")
        return value

    def clean_players_min_override(self):
        value = self.cleaned_data.get("players_min_override")
        if value is not None and value < 1:
            raise forms.ValidationError("Players min override must be at least 1.")
        return value

    def clean_players_max_override(self):
        value = self.cleaned_data.get("players_max_override")
        if value is not None and value < 1:
            raise forms.ValidationError("Players max override must be at least 1.")
        return value

    def clean_alternate_names(self):
        wanted, seen = [], set()
        for line in self.cleaned_data.get("alternate_names", "").splitlines():
            alt = line.strip()
            if alt and alt.lower() not in seen:
                seen.add(alt.lower())
                wanted.append(alt)
        return wanted


class SeriesForm(forms.ModelForm):
    """Shared by series_add and series_edit (issue #21/#54/#28). `members`
    and `primary_game` both edit the reverse side of Game.series, so neither
    is a Meta field — `primary_game` deliberately stays a plain declared
    field rather than a Meta field too: Series.clean() (models.py) guards
    "primary must be a member" against the *current DB* membership, which
    is only reconciled by the view *after* save(); if primary_game were a
    Meta field, ModelForm.is_valid() would run that guard against stale
    membership via instance.full_clean(). This form's own clean() enforces
    the same rule against the *posted* members instead, exactly like the
    old hand-parsed _save_series."""

    primary_game = forms.ModelChoiceField(queryset=Game.objects.none())
    members = forms.ModelMultipleChoiceField(
        queryset=Game.objects.none(), required=False)
    cover = forms.ImageField(required=False, widget=forms.FileInput(
        attrs={"class": "form-control", "accept": "image/*", "id": "series-cover"}))

    class Meta:
        model = Series
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "id": "series-name"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        series = self.instance if self.instance.pk else None
        # A game in ANOTHER series never shows, so the editor can't silently
        # steal it (issue #54); a game already on THIS series stays offered.
        unclaimed = Q(series__isnull=True)
        if series:
            unclaimed |= Q(series=series)
        candidates = Game.objects.filter(type=Game.Type.BASE).filter(unclaimed)
        self.fields["primary_game"].queryset = candidates
        self.fields["members"].queryset = candidates
        if series:
            self.initial.setdefault("primary_game", series.primary_game_id)
            self.initial.setdefault(
                "members", list(series.members.values_list("pk", flat=True)))
            del self.fields["cover"]

    def clean(self):
        cleaned = super().clean()
        primary, members = cleaned.get("primary_game"), cleaned.get("members")
        if primary is not None and members is not None and primary not in members:
            raise forms.ValidationError("Primary game must be one of the members.")
        return cleaned

    def clean_cover(self):
        upload = self.cleaned_data.get("cover")
        if upload:
            data = upload.read()
            error, _ = _validate_cover_image(data)
            if error:
                raise forms.ValidationError(error)
            self.cover_data = data
        return upload
