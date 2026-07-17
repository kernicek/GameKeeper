from django.contrib import admin
from django.urls import reverse
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Accessory, AccessoryCopy, BggLink, BggSyncDiff, CardSize, Copy,
    CopySleeveStatus, DigitalImplementation, Edition, ExternalLink, Family,
    Game, GameType, GameTag, Group, Invite, Loan, Location, Membership,
    PledgeManager, PledgePlan, PledgePlanBundle, PledgePlanItem,
    Product, ProductSleeveRequirement, Purchase, ReminderLog, Series,
    ShareGrant, SleeveInventory, SleeveProduct, SleeveRequirement, Tag, Wave,
)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0


class ShareGrantInline(admin.TabularInline):
    """DESIGN §3 tier 2: who this group's collection is shared with. Only
    effective while the group's visibility is "shared"."""

    model = ShareGrant
    fk_name = 'group'
    extra = 0


class InviteInline(admin.TabularInline):
    """DESIGN §3: pending invites for existing users to join this group."""

    model = Invite
    fk_name = 'group'
    extra = 0


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'visibility', 'viewer_link', 'share_link',
                    'created_at')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('viewer_link', 'share_link')
    inlines = [MembershipInline, ShareGrantInline, InviteInline]
    actions = ('enable_share_links', 'revoke_share_links')

    @admin.display(description='Viewer link (tiers 2/3)')
    def viewer_link(self, obj):
        return reverse('group_collection', args=[obj.slug])

    @admin.display(description='Anonymous share link')
    def share_link(self, obj):
        if obj.share_token:
            return reverse('share_collection', args=[obj.share_token])
        return '—'

    @admin.action(description='Enable anonymous share link (DESIGN §3 tier 4)')
    def enable_share_links(self, request, queryset):
        for group in queryset:
            group.enable_share_link()

    @admin.action(description='Revoke anonymous share link')
    def revoke_share_links(self, request, queryset):
        queryset.update(share_token=None)


class BggLinkInline(admin.TabularInline):
    model = BggLink
    extra = 1


class ExternalLinkInline(admin.TabularInline):
    model = ExternalLink
    extra = 0


class EditionInline(admin.TabularInline):
    model = Edition
    extra = 0


class GameTagInline(admin.TabularInline):
    model = GameTag
    extra = 0
    autocomplete_fields = ('tag',)


class GameTypeInline(admin.TabularInline):
    model = GameType
    extra = 0


class DigitalImplementationInline(admin.TabularInline):
    model = DigitalImplementation
    extra = 0


class SleeveRequirementInline(admin.TabularInline):
    model = SleeveRequirement
    extra = 0


class CopySleeveStatusInline(admin.TabularInline):
    model = CopySleeveStatus
    extra = 0


class LoanInline(admin.TabularInline):
    """Issue #43: a copy's lend/borrow history, editable alongside it."""

    model = Loan
    extra = 0


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('name', 'type', 'year_published', 'last_synced_at')
    list_filter = ('type', 'language_dependency', 'game_types__game_type')
    search_fields = ('name', 'bgg_name')
    inlines = [BggLinkInline, ExternalLinkInline, EditionInline,
               GameTagInline, GameTypeInline, DigitalImplementationInline]


@admin.register(Series)
class SeriesAdmin(admin.ModelAdmin):
    """Minimal fallback surface — members and the primary are normally
    managed in the in-app series editor (issue #21)."""

    list_display = ('name', 'primary_game', 'member_count')
    search_fields = ('name', 'members__name')
    autocomplete_fields = ('primary_game',)

    @admin.display(description='Members')
    def member_count(self, obj):
        return obj.members.count()


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    """Minimal fallback surface — members are normally managed in the
    in-app family editor (issue #42)."""

    list_display = ('name', 'bgg_family_id', 'member_count')
    search_fields = ('name', 'members__name')

    @admin.display(description='Members')
    def member_count(self, obj):
        return obj.members.count()


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'kind')
    list_filter = ('kind',)
    search_fields = ('name',)


@admin.register(Edition)
class EditionAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'is_default', 'is_pnp', 'size_category', 'num_boxes')
    list_filter = ('is_default', 'is_pnp', 'size_category')
    search_fields = ('game__name', 'name')
    inlines = [SleeveRequirementInline]


@admin.register(Copy)
class CopyAdmin(SimpleHistoryAdmin):
    list_display = ('__str__', 'owner', 'excitement', 'keep_status',
                    'archive_status', 'location', 'is_borrowed_in')
    list_filter = ('archive_status', 'keep_status', 'immune', 'is_borrowed_in')
    search_fields = ('edition__game__name', 'owner__username')
    autocomplete_fields = ('edition',)
    inlines = [CopySleeveStatusInline, LoanInline]


@admin.register(CardSize)
class CardSizeAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'width_mm', 'height_mm', 'aliases')
    search_fields = ('name', 'aliases')


@admin.register(SleeveProduct)
class SleeveProductAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'brand', 'card_size', 'pack_size', 'finish', 'back')
    list_filter = ('brand',)
    search_fields = ('brand', 'name')


@admin.register(SleeveInventory)
class SleeveInventoryAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'owner', 'packs', 'loose')
    list_filter = ('owner',)


@admin.register(Accessory)
class AccessoryAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'brand', 'game', 'edition', 'bgg_id')
    list_filter = ('brand',)
    search_fields = ('name', 'brand', 'game__name')
    autocomplete_fields = ('game', 'edition')


@admin.register(AccessoryCopy)
class AccessoryCopyAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'owner', 'accessory', 'acquired_date')
    list_filter = ('owner',)
    search_fields = ('accessory__name', 'owner__username')
    autocomplete_fields = ('accessory',)


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ('name', 'group', 'type', 'share_link')
    list_filter = ('type',)
    readonly_fields = ('share_link',)
    actions = ('enable_share_links', 'revoke_share_links')

    @admin.display(description='Share link (issue #123)')
    def share_link(self, obj):
        if obj.share_token:
            return reverse('share_location_collection', args=[obj.share_token])
        return '—'

    @admin.action(description='Enable location share link (issue #123)')
    def enable_share_links(self, request, queryset):
        for location in queryset:
            location.enable_share_link()

    @admin.action(description='Revoke location share link')
    def revoke_share_links(self, request, queryset):
        queryset.update(share_token=None)


class WaveInline(admin.TabularInline):
    model = Wave
    extra = 0


class ProductInline(admin.TabularInline):
    model = Product
    extra = 0
    fields = ('name', 'kind', 'game', 'copy', 'accessory_copy')
    autocomplete_fields = ('game', 'copy', 'accessory_copy')


class ProductSleeveRequirementInline(admin.TabularInline):
    model = ProductSleeveRequirement
    extra = 0


@admin.register(PledgeManager)
class PledgeManagerAdmin(admin.ModelAdmin):
    list_display = ('name', 'default_url')
    search_fields = ('name',)


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'platform', 'status', 'ordered_date',
                    'pledge_manager_status', 'excitement')
    list_filter = ('platform', 'status', 'pledge_manager', 'pledge_manager_status')
    search_fields = ('name',)
    inlines = [WaveInline]


class PledgePlanItemInline(admin.TabularInline):
    model = PledgePlanItem
    extra = 0


class PledgePlanBundleInline(admin.TabularInline):
    model = PledgePlanBundle
    extra = 0
    filter_horizontal = ('items',)


@admin.register(PledgePlan)
class PledgePlanAdmin(admin.ModelAdmin):
    list_display = ('purchase', 'currency', 'vat_rate', 'czk_rate')
    search_fields = ('purchase__name',)
    inlines = [PledgePlanItemInline, PledgePlanBundleInline]


@admin.register(Wave)
class WaveAdmin(SimpleHistoryAdmin):
    list_display = ('__str__', 'delivery_type', 'status', 'original_eta',
                    'expected_arrival', 'arrived_date', 'address')
    list_filter = ('status', 'delivery_type')
    search_fields = ('purchase__name',)
    inlines = [ProductInline]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'wave', 'kind', 'game', 'copy')
    list_filter = ('kind',)
    search_fields = ('name', 'wave__purchase__name')
    autocomplete_fields = ('game', 'edition', 'copy')
    inlines = [ProductSleeveRequirementInline]


@admin.register(ReminderLog)
class ReminderLogAdmin(admin.ModelAdmin):
    """Read-only audit of §11 reminder emails. Deleting a row re-arms that
    reminder (next beat run emails again) — that's the supported "resend"."""

    list_display = ('purchase', 'kind', 'deadline', 'sent_at')
    list_filter = ('kind',)
    search_fields = ('purchase__name',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BggSyncDiff)
class BggSyncDiffAdmin(admin.ModelAdmin):
    """Read-only record of §8 sync diffs. The sync owns the rows; deleting
    one makes the next sync recreate it unreviewed — that's the supported
    "un-dismiss"."""

    list_display = ('owner', 'category', 'game', 'bgg_id', 'bgg_name',
                    'last_seen_at', 'dismissed_at')
    list_filter = ('category',)
    search_fields = ('game__name', 'bgg_name')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ShareGrant)
class ShareGrantAdmin(admin.ModelAdmin):
    list_display = ('group', 'grantee_user', 'grantee_group', 'created_at')
    list_filter = ('group',)


admin.site.register(Membership)
admin.site.register(Invite)
