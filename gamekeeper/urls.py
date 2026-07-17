from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # The collection is the home page (issue #7); the dashboard lives under
    # its own prefix. URL *names* are unchanged, so reverse() callers and
    # LOGIN_REDIRECT_URL ('/') keep working.
    path('', views.collection, name='collection'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/shortfall/', views.shortfall_partial, name='shortfall_partial'),
    # Full-list pages behind each dashboard card (issue #83).
    path('dashboard/incoming-waves/', views.dashboard_incoming_waves,
         name='dashboard_incoming_waves'),
    path('dashboard/pledge-managers/', views.dashboard_pledge_managers,
         name='dashboard_pledge_managers'),
    path('dashboard/campaigns-ending/', views.dashboard_campaigns_ending,
         name='dashboard_campaigns_ending'),
    path('dashboard/sync-diffs/', views.dashboard_sync_diffs,
         name='dashboard_sync_diffs'),
    path('dashboard/sync-diffs/<int:pk>/dismiss/', views.sync_diff_dismiss,
         name='sync_diff_dismiss'),
    path('dashboard/sync-diffs/<int:pk>/accept/', views.sync_diff_accept,
         name='sync_diff_accept'),
    path('dashboard/to-craft/', views.dashboard_to_craft,
         name='dashboard_to_craft'),
    # Issue #64: new-expansion tracking + the wishlist it feeds.
    path('dashboard/new-expansions/', views.dashboard_new_expansions,
         name='dashboard_new_expansions'),
    path('dashboard/new-expansions/<int:pk>/dismiss/', views.new_expansion_dismiss,
         name='new_expansion_dismiss'),
    path('dashboard/new-expansions/<int:pk>/wishlist/', views.wishlist_add,
         name='wishlist_add'),
    path('wishlist/', views.wishlist_list, name='wishlist'),
    path('wishlist/<int:pk>/remove/', views.wishlist_remove, name='wishlist_remove'),
    # Old /collection/ bookmarks (and hx-push-url'd filter links) predate
    # the swap — send them home, filters intact.
    path('collection/', RedirectView.as_view(pattern_name='collection',
                                             query_string=True, permanent=True)),
    # Issue #90: superuser Tools page — trigger the bulk BGG sync and cover
    # download in-app instead of over the shell.
    path('tools/', views.tools, name='tools'),
    path('tools/run/<str:kind>/', views.tools_run, name='tools_run'),
    path('tools/status/', views.tools_status, name='tools_status'),
    path('curation/', views.curation, name='curation'),
    path('curation/copies/<int:pk>/', views.curation_edit, name='curation_edit'),
    path('curation/copies/<int:pk>/archive/', views.curation_archive,
         name='curation_archive'),
    path('curation/archived/', views.archived_copies, name='archived_copies'),
    path('sleeves/', views.sleeves, name='sleeves'),
    path('sleeves/inventory/<int:product_pk>/', views.sleeve_inventory_edit,
         name='sleeve_inventory_edit'),
    path('sleeves/copies/<int:copy_pk>/sizes/<int:size_pk>/',
         views.sleeve_status_edit, name='sleeve_status_edit'),
    path('purchases/', views.purchases, name='purchases'),
    path('purchases/add/', views.purchase_add, name='purchase_add'),
    path('purchases/<int:pk>/', views.purchase_detail, name='purchase_detail'),
    path('purchases/<int:pk>/edit/', views.purchase_edit, name='purchase_edit'),
    path('purchases/<int:pk>/waves/add/', views.wave_add, name='wave_add'),
    path('waves/<int:pk>/products/add/', views.product_add, name='product_add'),
    # Issue #38: read-only item page, linked from the purchase edit table.
    path('products/<int:pk>/', views.product_detail, name='product_detail'),
    path('products/<int:pk>/edit/', views.product_edit, name='product_edit'),
    path('products/<int:pk>/convert/', views.product_convert,
         name='product_convert'),
    # Issue #186: pledge-level decision planner, scoped to one purchase.
    path('purchases/<int:purchase_pk>/pledge-plan/add/', views.pledge_plan_add,
         name='pledge_plan_add'),
    path('pledge-plan/<int:pk>/', views.pledge_plan_detail,
         name='pledge_plan_detail'),
    path('pledge-plan/<int:pk>/edit/', views.pledge_plan_edit,
         name='pledge_plan_edit'),
    path('pledge-plan/<int:pk>/items/add/', views.pledge_plan_item_add,
         name='pledge_plan_item_add'),
    path('pledge-plan-items/<int:pk>/edit/', views.pledge_plan_item_edit,
         name='pledge_plan_item_edit'),
    path('pledge-plan-items/<int:pk>/delete/', views.pledge_plan_item_delete,
         name='pledge_plan_item_delete'),
    path('pledge-plan/<int:pk>/bundles/add/', views.pledge_plan_bundle_add,
         name='pledge_plan_bundle_add'),
    path('pledge-plan-bundles/<int:pk>/edit/', views.pledge_plan_bundle_edit,
         name='pledge_plan_bundle_edit'),
    path('pledge-plan-bundles/<int:pk>/delete/', views.pledge_plan_bundle_delete,
         name='pledge_plan_bundle_delete'),
    path('pledge-plan-bundles/<int:pk>/items/<int:item_pk>/toggle/',
         views.pledge_plan_bundle_item_toggle, name='pledge_plan_bundle_item_toggle'),
    path('pledge-plan-bundles/<int:pk>/shortlist/',
         views.pledge_plan_bundle_shortlist_toggle, name='pledge_plan_bundle_shortlist_toggle'),
    # Issue #55: create a game from just a BGG id (or pasted BGG URL).
    path('games/add/', views.game_add, name='game_add'),
    # Issue #81: bulk-import the BGG collection with a preview step.
    path('games/import/', views.bgg_import, name='bgg_import'),
    # Issue #137: the general Settings page; its first section is the per-user
    # BGG account (username + encrypted password, issue #118).
    path('settings/', views.settings_page, name='settings'),
    # Issue #162: send a one-off test push to the saved ntfy topic.
    path('settings/ntfy/test/', views.settings_ntfy_test, name='settings_ntfy_test'),
    # Issue #171: send a one-off test email to the user's account address.
    path('settings/email/test/', views.settings_email_test, name='settings_email_test'),
    # Issue #61: accept/decline a pending Invite to join a household.
    path('invites/<int:pk>/accept/', views.invite_accept, name='invite_accept'),
    path('invites/<int:pk>/decline/', views.invite_decline, name='invite_decline'),
    # Issue #65: read-only plays history feed (all games, or ?game=<pk>).
    path('plays/', views.plays, name='plays'),
    path('games/<int:pk>/', views.game_detail, name='game_detail'),
    path('games/<int:pk>/edit/', views.game_edit, name='game_edit'),
    path('games/<int:pk>/sync/', views.game_bgg_sync, name='game_bgg_sync'),
    path('games/<int:pk>/copies/add/', views.copy_add, name='copy_add'),
    # Issue #43: "I'm borrowing this" — the reverse of copy_add.
    path('games/<int:pk>/copies/add-borrowed/', views.copy_add_borrowed,
         name='copy_add_borrowed'),
    path('copies/<int:pk>/edit/', views.copy_edit, name='copy_edit'),
    path('copies/<int:pk>/mark-ready/', views.copy_mark_ready,
         name='copy_mark_ready'),
    path('copies/<int:pk>/loan-out/', views.copy_loan_out, name='copy_loan_out'),
    path('copies/<int:pk>/loan-return/', views.copy_loan_return,
         name='copy_loan_return'),
    # Issue #53: in-app edition editor, linked from the game detail page.
    path('games/<int:pk>/editions/add/', views.edition_add, name='edition_add'),
    path('editions/<int:pk>/edit/', views.edition_edit, name='edition_edit'),
    # Issue #129: in-app sleeve-requirement editor on the edition edit page.
    path('editions/<int:pk>/requirements/add/', views.requirement_add,
         name='requirement_add'),
    path('requirements/<int:pk>/edit/', views.requirement_edit,
         name='requirement_edit'),
    path('requirements/<int:pk>/delete/', views.requirement_delete,
         name='requirement_delete'),
    # DESIGN §7 documents (issue #60): attach rulebooks/PnP/references to a
    # game. Add hangs off the game; edit/delete off the document (issue #97
    # moved priority + delete onto the edit page).
    path('games/<int:pk>/documents/add/', views.document_add,
         name='document_add'),
    path('documents/<int:pk>/edit/', views.document_edit,
         name='document_edit'),
    path('documents/<int:pk>/delete/', views.document_delete,
         name='document_delete'),
    path('games/<int:pk>/cover/', views.game_cover_edit, name='game_cover_edit'),
    path('games/<int:pk>/cover/focus/', views.game_cover_focus,
         name='game_cover_focus'),
    # DESIGN §4 series (issue #21): detail page + in-app editor.
    # Issue #80: overview grid of all series.
    path('series/', views.series_list, name='series_list'),
    path('series/add/', views.series_add, name='series_add'),
    path('series/<int:pk>/', views.series_detail, name='series_detail'),
    # Issue #58: bulk-move the current user's copies of the members.
    path('series/<int:pk>/location/', views.series_set_location,
         name='series_set_location'),
    path('series/<int:pk>/edit/', views.series_edit, name='series_edit'),
    # Issue #54: the shared cover editor, series flavour.
    path('series/<int:pk>/cover/', views.series_cover_edit,
         name='series_cover_edit'),
    path('series/<int:pk>/cover/focus/', views.series_cover_focus,
         name='series_cover_focus'),
    # DESIGN §4 family (issue #42): detail page + in-app editor + shared
    # cover editor, family flavour.
    # Issue #80: overview grid of all families.
    path('families/', views.family_list, name='family_list'),
    path('families/add/', views.family_add, name='family_add'),
    path('families/<int:pk>/', views.family_detail, name='family_detail'),
    path('families/<int:pk>/edit/', views.family_edit, name='family_edit'),
    path('families/<int:pk>/cover/', views.family_cover_edit,
         name='family_cover_edit'),
    path('families/<int:pk>/cover/focus/', views.family_cover_focus,
         name='family_cover_focus'),
    # DESIGN §3 tier 4: anonymous share link. <slug:> matches token_urlsafe's
    # [-A-Za-z0-9_] alphabet.
    path('share/<slug:token>/', views.share_collection, name='share_collection'),
    path('share/<slug:token>/games/<int:pk>/', views.share_game_detail,
         name='share_game_detail'),
    # Issue #123: share link pinned to one Location, no login required —
    # 'share/location/...' can't collide with 'share/<slug:token>/' above
    # since <slug:> never matches '/'.
    path('share/location/<slug:token>/', views.share_location_collection,
         name='share_location_collection'),
    path('share/location/<slug:token>/games/<int:pk>/',
         views.share_location_game_detail, name='share_location_game_detail'),
    # DESIGN §3 tiers 2+3: logged-in viewers browse a granted / server-public
    # group collection by its slug.
    path('g/<slug:slug>/', views.group_collection, name='group_collection'),
    path('g/<slug:slug>/games/<int:pk>/', views.group_game_detail,
         name='group_game_detail'),
    # DESIGN §3 owner-facing sharing settings: visibility tier, grants and
    # the anonymous share link, managed without the admin.
    path('g/<slug:slug>/settings/', views.group_settings,
         name='group_settings'),
    path('g/<slug:slug>/settings/visibility/', views.group_settings_visibility,
         name='group_settings_visibility'),
    path('g/<slug:slug>/settings/share-link/', views.group_settings_share_link,
         name='group_settings_share_link'),
    path('g/<slug:slug>/settings/locations/<int:location_pk>/share-link/',
         views.group_settings_location_share_link,
         name='group_settings_location_share_link'),
    path('g/<slug:slug>/settings/grants/', views.group_settings_grant_add,
         name='group_settings_grant_add'),
    path('g/<slug:slug>/settings/grants/<int:pk>/delete/',
         views.group_settings_grant_delete, name='group_settings_grant_delete'),
    path('g/<slug:slug>/settings/invites/', views.group_settings_invite_add,
         name='group_settings_invite_add'),
    path('g/<slug:slug>/settings/invites/<int:pk>/delete/',
         views.group_settings_invite_delete, name='group_settings_invite_delete'),
]
