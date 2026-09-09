"""
API v1 Router

Main router that aggregates all API v1 endpoints.
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_admin_or_owner, require_volet
from app.api.v1.endpoints import (
    devlog_notes_ai,
    devlog_project_finances,
    devlog_project_members,
    devlog_project_phases,
    devlog_project_photos,
    devlog_project_purchases,
    devlog_project_recap,
    devlog_project_recurring_services,
    devlog_project_tasks,
)
from app.api.v1.endpoints import (
    admin_data,
    agenda_availability,
    agenda_unified,
    appointment_types,
    letmetalk,
    webhooks_meta,
    activity,
    ai,
    api_keys,
    assistant,
    mon_ia,
    appointments,
    audit,
    auth,
    blog,
    calendar,
    clients,
    cockpit,
    construction_bon_defaults,
    devlog,
    devlog_soumission_defaults,
    drive_auth,
    drive_auto_uploads,
    drive_conventions,
    drive_files,
    drive_page_modules,
    employes,
    entreprises,
    esign,
    extension,
    follow_ups,
    contact,
    contacts,
    copilote,
    crm_columns,
    relances,
    entreprise_extras,
    entreprise_partners_links,
    contrats_gestion,
    immobilier,
    immobilier_communications,
    immobilier_documents,
    immobilier_exports,
    immobilier_extras,
    immobilier_import_excel,
    immobilier_locations,
    immobilier_assurances,
    immobilier_docs_perso,
    immobilier_gestion_externe,
    immobilier_frais_gestion,
    immobilier_releves31,
    immobilier_validation_bancaire,
    investissements,
    invest_admin,
    invest_portal,
    dashboard,
    help,
    kratos,
    ndas,
    offers,
    org_nodes,
    raci,
    org_seed_canonical,
    optimisation,
    rencontres,
    rencontres_teams,
    timesheets,
    achat_receipt,
    bon_items,
    bon_photos,
    bon_send,
    bons_refs,
    facture_import,
    facture_items,
    facture_qbo,
    facture_send,
    leave_requests,
    measurements,
    mobile,
    notifications,
    payments,
    permissions,
    public_nda,
    public_offer,
    sales_tasks,
    service_templates,
    user_roles,
    users,
    project_billables,
    project_finances,
    project_members,
    project_phases,
    project_photos,
    project_punches,
    project_tasks,
    project_to_facture,
    projects,
    public_document,
    public_esign,
    public_bon,
    public_contrat_gestion,
    public_devlog_contact,
    public_devlog_invoice,
    public_devlog_nps,
    push,
    public_contract,
    public_devlog_soumission,
    public_facture,
    public_purchase_agreement,
    public_soumission,
    purchase_agreement_milestones,
    purchase_agreement_template,
    purchase_agreements,
    punch_ops,
    achat_qbo,
    achat_payment,
    email_templates,
    mtl_properties,
    comparables,
    subscriptions,
    lead_analyses,
    prospection,
    prospection_analyse_extract,
    prospection_analyses,
    prospection_analysis_defaults,
    prospection_deals,
    prospection_lists,
    rental_comparables,
    purchase_order_actions,
    purchase_order_items,
    client_qbo,
    cron_runner,
    automations,
    acquisition,
    numbering,
    qbo_account_map,
    qbo_bulk,
    qbo_oauth,
    qbo_token,
    qbo_webhook,
    search,
    contract_sign,
    soumission_items,
    soumission_qbo,
    soumission_send,
    soumission_status,
    soumission_to_client,
    soumission_to_project,
    soumissions_aggregates,
    subcontractor_contracts,
    voice,
    webhooks,
)
from app.api.v1.endpoints.business import (
    achats_router,
    purchase_orders_router,
    agenda_router,
    bons_router,
    factures_router,
    fournisseurs_router,
    punch_router,
    soumissions_router,
    sous_traitants_router,
    sous_traitant_timesheets_router,
    note_templates_router,
)


# ── Gardes d'accès par pôle (refonte permissions 2026-07) ──────────────────
# Un routeur métier reçoit la garde de SON pôle ; une ressource partagée
# accepte l'un OU l'autre volet (User.has_volet : owner/admin = tous les
# volets). Les routeurs transverses (auth, users, drive, agenda, …), les
# PUBLICS (public_*, webhooks, cron, extension…) et les routeurs devlog
# (déjà gardés owner/admin via _devlog_admin_only) n'en reçoivent aucune.
DEP_CONSTRUCTION = [Depends(require_volet("construction"))]
DEP_CONSTRUCTION_IMMO = [Depends(require_volet("construction", "immobilier"))]
DEP_PROSPECTION = [Depends(require_volet("prospection"))]
DEP_PROSPECTION_INVEST = [
    Depends(require_volet("prospection", "investisseur"))
]
DEP_IMMOBILIER = [Depends(require_volet("immobilier"))]
DEP_ENTREPRISES = [Depends(require_volet("entreprises"))]
DEP_INVESTISSEUR = [Depends(require_volet("investisseur"))]

api_router = APIRouter()

# Core
api_router.include_router(auth.router)
api_router.include_router(users.router)
# Clés d'API personnelles (gestion via JWT) + activité du compte
# (lecture seule via clé d'API krts_...). Socle « Temps 1 » pour les
# agents externes de Phil. api_keys = CRUD des clés (auth JWT) ;
# activity = lecture de l'activité du compte (auth par clé d'API).
api_router.include_router(api_keys.router)
api_router.include_router(activity.router)
# Assistant IA (phase 1) : catalogue d'outils + cartes d'action à
# confirmer. Garde JWT standard ; la permission de PAGE de chaque outil
# est vérifiée outil par outil (catalogue filtré = exécution refusée) —
# pas de garde de volet au niveau du routeur, l'assistant est transverse.
api_router.include_router(assistant.router)
# IA personnelle (« chacun son IA ») — transverse, aucune garde de
# volet : chacun gère SA connexion.
api_router.include_router(mon_ia.router)
api_router.include_router(clients.router)
# Le pôle Dev Logiciel est restreint à admin/owner (Phil + Steven).
# La garde est appliquée à TOUS les routers internes du pôle ; seuls
# les routers publics (signature contrat sans login, formulaire de
# contact, signature soumission, consultation facture) restent ouverts.
_devlog_admin_only = [Depends(get_current_admin_or_owner)]
api_router.include_router(devlog.clients_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.leads_router, dependencies=_devlog_admin_only)
# Automations DOIT être registered AVANT soumissions_router pour que
# le PATCH override (auto-création projet quand status → acceptee) et
# /soumissions/{id}/convert-to-project matchent avant le CRUD générique.
api_router.include_router(devlog.soumission_automations_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.soumissions_router, dependencies=_devlog_admin_only)
# Phase 6 (juin 2026) — Valeurs par défaut CONFIGURABLES des soumissions
# devis_dev (taux, marges, commission closer, template modules/fonctionnalités
# de base). Prefix statique /devlog/soumission-defaults : pas de collision avec
# le CRUD /devlog/soumissions/{id}. Admin/owner appliqué dans les routes.
api_router.include_router(devlog_soumission_defaults.router)
api_router.include_router(devlog.projects_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.time_entries_router, dependencies=_devlog_admin_only)
# invoice_automations_router DOIT être registered AVANT invoices_router
# pour que /devlog/invoices/{id}/send, /pdf et /mark-paid matchent
# avant le CRUD générique /devlog/invoices/{item_id}.
api_router.include_router(devlog.invoice_automations_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.invoices_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.soumission_items_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.soumission_sections_router, dependencies=_devlog_admin_only)
# Niveau MODULE (refonte 2026-06) — CRUD/reorder/assign + lecture
# hiérarchique sections → modules → items. Routes statiques
# (/soumission-modules, /soumissions/{id}/structure) : pas de collision
# avec le CRUD générique des soumissions.
api_router.include_router(devlog.soumission_modules_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.related_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.sous_traitants_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.invoice_items_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.lead_needs_router, dependencies=_devlog_admin_only)
api_router.include_router(devlog.contracts_router, dependencies=_devlog_admin_only)
# public_contracts_router = signature de contrat sans auth (prefix /public/devlog).
# Ne PAS protéger sinon les clients externes ne peuvent plus signer.
api_router.include_router(devlog.public_contracts_router)
# Page publique signature soumission devis_dev — /public/devlog/soumissions/{token}
api_router.include_router(public_devlog_soumission.router)
# Page publique consultation facture devlog — /public/devlog/invoices/{token}
api_router.include_router(public_devlog_invoice.router)
# Page publique NPS post-livraison — /public/devlog/nps/{token}
api_router.include_router(public_devlog_nps.router)
# Nested project routes MUST be registered before projects.router so
# /projects/{id}/photos etc. are matched before /projects/{item_id}.
api_router.include_router(project_photos.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_tasks.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_phases.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_phases.phases_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_members.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_finances.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_punches.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_billables.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(subcontractor_contracts.router)
api_router.include_router(projects.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(contact.router)
api_router.include_router(contacts.router)
api_router.include_router(crm_columns.router)
api_router.include_router(relances.router)
api_router.include_router(copilote.router)
api_router.include_router(blog.router)
api_router.include_router(webhooks.router)
api_router.include_router(voice.router)
api_router.include_router(public_soumission.router)
api_router.include_router(public_contract.router)
api_router.include_router(public_facture.router)
api_router.include_router(contract_sign.router)
api_router.include_router(contract_sign.docs_router)
api_router.include_router(public_bon.router)
api_router.include_router(public_document.router)
# eSign public — signature de documents (pôle Gestion d'entreprise).
# Ne PAS protéger : les signataires externes n'ont pas de compte.
api_router.include_router(public_esign.router)
api_router.include_router(public_contrat_gestion.router)
api_router.include_router(push.router)
api_router.include_router(appointment_types.router)
api_router.include_router(agenda_availability.router)
api_router.include_router(user_roles.router)
api_router.include_router(permissions.router)
api_router.include_router(letmetalk.router)
api_router.include_router(webhooks_meta.router)
# Webhook QBO → Kratos (reverse-sync). Public (appelé par Intuit), protégé
# par la signature intuit-signature vérifiée dans l'endpoint.
api_router.include_router(qbo_webhook.router)
api_router.include_router(public_purchase_agreement.router)
# Offre d'achat minimaliste — flow indépendant du PurchaseAgreement
# complet (modèle Offer dédié, page publique /sign-offer/{token}).
api_router.include_router(offers.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(public_offer.router)
# NDA investisseurs — entente de confidentialité minimaliste pour
# partager les infos d'un deal Pipeline avec un investisseur
# potentiel (modèle NDA dédié, page publique /sign-nda/{token}).
api_router.include_router(ndas.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(public_nda.router)
api_router.include_router(qbo_token.router)
api_router.include_router(qbo_bulk.router)
api_router.include_router(qbo_oauth.router)
# Drive OAuth Phase 1 (juin 2026) — connexion Google par utilisateur,
# tokens chiffrés en BDD. Pré-requis pour les Phases 2-7 (wrapper Drive
# API, composant <DriveFolderExplorer>, conventions, auto-upload).
api_router.include_router(drive_auth.router)
# Drive API wrapper Phase 2 — opérations CRUD Drive (list, upload,
# download, rename, move, trash, restore, create folder, copy
# recursive, search, share). Tous protégés admin/owner. Cf.
# docs/DRIVE_INTEGRATION.md.
api_router.include_router(drive_files.router)
# Drive Conventions Phase 4 — CRUD des règles "entité Kratos → dossier
# Drive" + action manuelle d'application + CRUD des DriveEntityLink.
# Admin/owner only. Pas de hook automatique côté SQLAlchemy ici (Phase 5).
api_router.include_router(drive_conventions.router)
# Drive Page Modules Phase 7 — activation par type de page de la
# section Drive (<EntityDriveSection>). GET status consommé par les
# pages d'entités, PATCH/POST/list réservés admin/owner. Cf.
# docs/DRIVE_INTEGRATION.md.
api_router.include_router(drive_page_modules.router)
# Drive Auto-Upload Phase 6 — CRUD des règles "document généré →
# sous-dossier Drive de l'entité" (admin/owner). Le dispatcher
# (drive_auto_upload_dispatcher) consomme ces règles depuis les endpoints
# de génération de documents. Cf. docs/DRIVE_INTEGRATION.md.
api_router.include_router(drive_auto_uploads.router)
api_router.include_router(client_qbo.router)
api_router.include_router(achat_qbo.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(achat_payment.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(purchase_order_actions.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(cron_runner.router)
api_router.include_router(automations.router)
api_router.include_router(acquisition.router, dependencies=DEP_PROSPECTION)
api_router.include_router(numbering.router)
api_router.include_router(qbo_account_map.router)
api_router.include_router(extension.router)
api_router.include_router(dashboard.router)
api_router.include_router(calendar.router)
api_router.include_router(appointments.router)
api_router.include_router(measurements.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(sales_tasks.router, dependencies=DEP_PROSPECTION)
api_router.include_router(mobile.router)
api_router.include_router(leave_requests.router)
api_router.include_router(service_templates.router)
api_router.include_router(search.router)
api_router.include_router(notifications.router)
api_router.include_router(audit.router)
api_router.include_router(follow_ups.router, dependencies=DEP_PROSPECTION)

# Business
api_router.include_router(employes.router)
api_router.include_router(fournisseurs_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(sous_traitants_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(sous_traitant_timesheets_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(note_templates_router)
api_router.include_router(soumissions_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_items.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_qbo.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_send.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_status.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumissions_aggregates.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_to_client.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(soumission_to_project.router, dependencies=DEP_CONSTRUCTION)
# /agenda/unified DOIT être avant agenda_router (CRUD générique avec
# /agenda/{item_id}) pour que le path littéral matche en premier.
api_router.include_router(agenda_unified.router)
api_router.include_router(agenda_router)
api_router.include_router(bon_items.router, dependencies=DEP_CONSTRUCTION_IMMO)
# Photos d'un bon : la MEME porte que le reste du bon (construction OU
# immobilier). Avant 2026-08-21, seules les routes /immobilier/... existaient
# et exigeaient le volet immobilier : un charge de projet construction ne
# pouvait pas joindre de photo.
api_router.include_router(bon_photos.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(bon_send.router, dependencies=DEP_CONSTRUCTION_IMMO)
# /bons/refs/* AVANT bons_router : ses chemins littéraux doivent matcher
# avant les routes génériques /bons/{id}. Listes immeubles/logements du
# formulaire de bon, sans exiger le volet immobilier.
api_router.include_router(bons_refs.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(bons_router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(construction_bon_defaults.router, dependencies=DEP_CONSTRUCTION)
# punch_ops FIRST so its literal paths (/me, /debug, /weekly, ...)
# are matched before the generic /{item_id} from punch_router, which
# would otherwise try to coerce "me"/"debug"/"weekly" to an int and
# return 422.
api_router.include_router(punch_ops.router, dependencies=DEP_CONSTRUCTION)
# Cockpit chargé de projet — vue d'ensemble Construction (manager+).
api_router.include_router(cockpit.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(punch_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(factures_router, dependencies=DEP_CONSTRUCTION)
# Payments sub-routes must come BEFORE facture_items so /factures/{id}/payments
# is matched before /factures/{id}/items (same prefix, different path).
api_router.include_router(payments.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(facture_items.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(facture_import.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(facture_send.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(facture_qbo.router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(project_to_facture.router, dependencies=DEP_CONSTRUCTION)
# achat_receipt must come BEFORE achats_router so /achats/{id}/receipt
# is matched before the generic /achats/{item_id} tries to parse
# "receipt" as an integer.
api_router.include_router(achat_receipt.router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(achats_router, dependencies=DEP_CONSTRUCTION_IMMO)
api_router.include_router(purchase_orders_router, dependencies=DEP_CONSTRUCTION)
api_router.include_router(purchase_order_items.router, dependencies=DEP_CONSTRUCTION)
# prospection_lists DOIT être registered AVANT prospection.router
# pour que /prospection/lists/* soit matché avant /prospection/{lead_id}.
# Idem pour mtl_properties qui a /prospection/mtl-properties/* et
# prospection_analyses qui a /prospection/analyses/* — match littéral
# avant /prospection/{lead_id}.
api_router.include_router(prospection_lists.router, dependencies=DEP_PROSPECTION)
# rental_comparables : prefix /prospection/rental-comparables, doit
# matcher avant /prospection/{lead_id} comme les autres.
api_router.include_router(rental_comparables.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(mtl_properties.router, dependencies=DEP_PROSPECTION_INVEST)
# Comparables de vente (« comps ») — prefix /prospection/comparables.
# Doit matcher AVANT prospection.router (/prospection/{lead_id}) comme
# les autres sous-routes littérales de /prospection.
api_router.include_router(comparables.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(subscriptions.router)
# /prospection/analyses/extract DOIT être avant prospection_analyses
# pour que le path littéral matche avant /prospection/analyses/{id}.
api_router.include_router(prospection_analyse_extract.router, dependencies=DEP_PROSPECTION)
api_router.include_router(prospection_analyses.router, dependencies=DEP_PROSPECTION)
# /prospection/analysis-defaults — admin/owner only, defaults
# globaux pour les inputs manuels du calculateur (taux refi, % MDF,
# taux prêteur B). Doit matcher AVANT /prospection/{lead_id}.
api_router.include_router(prospection_analysis_defaults.router, dependencies=DEP_PROSPECTION)
api_router.include_router(lead_analyses.router, dependencies=DEP_PROSPECTION)
# /prospection/deals DOIT être avant prospection.router pour la même
# raison que les autres : éviter la collision avec /prospection/{lead_id}.
api_router.include_router(prospection_deals.router, dependencies=DEP_PROSPECTION)
# purchase_agreements + pa-milestones DOIVENT être avant prospection.router
# pour que /prospection/{lead_id}/purchase-agreements et /prospection/pa-milestones
# matchent avant /prospection/{lead_id}.
api_router.include_router(purchase_agreements.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(purchase_agreement_milestones.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(purchase_agreement_template.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(prospection.router, dependencies=DEP_PROSPECTION_INVEST)
api_router.include_router(email_templates.router)
api_router.include_router(admin_data.router)
api_router.include_router(help.router)
api_router.include_router(kratos.router)
api_router.include_router(org_nodes.router)
api_router.include_router(raci.router)
api_router.include_router(org_seed_canonical.router)
# teams-sync AVANT rencontres : /rencontres/teams-sync/* ne doit pas
# être avalé par /rencontres/{id}.
api_router.include_router(optimisation.router, dependencies=DEP_ENTREPRISES)
api_router.include_router(rencontres_teams.router)
api_router.include_router(rencontres.router)
api_router.include_router(timesheets.router)
api_router.include_router(ai.router)
# eSign admin — signature électronique de documents (sidemenu QG →
# Signature). Prefix statique /esign : pas de collision avec le CRUD
# /entreprises/{id}.
api_router.include_router(esign.router, dependencies=DEP_ENTREPRISES)
api_router.include_router(entreprises.router, dependencies=DEP_ENTREPRISES)
# entreprise_extras DOIT être registered avant entreprises.router pour que
# /entreprises/finance/* et /entreprises/value-plans/* matchent avant
# /entreprises/{id}.
api_router.include_router(entreprise_extras.router, dependencies=DEP_ENTREPRISES)
# Partners + links DOIT être avant entreprises.router pour matcher
# /entreprises/{id}/partners et /entreprises/{id}/links avant
# /entreprises/{id} (PATCH).
api_router.include_router(entreprise_partners_links.router, dependencies=DEP_ENTREPRISES)
# immobilier_extras DOIT être registered avant immobilier.router pour que
# /immobilier/tal/* et /immobilier/renouvellements/* matchent avant les
# routes plus génériques de /immobilier.
api_router.include_router(immobilier_extras.router, dependencies=DEP_IMMOBILIER)
# Pipeline « Locations » (relocation) — avant immobilier.router aussi.
api_router.include_router(
    immobilier_locations.router, dependencies=DEP_IMMOBILIER
)
# Import Excel d'un immeuble complet (modèle + upload tout-ou-rien).
api_router.include_router(
    immobilier_import_excel.router, dependencies=DEP_IMMOBILIER
)
# Documents locatifs conservés (avis TAL, trousse) + envoi signature.
api_router.include_router(
    immobilier_documents.router, dependencies=DEP_IMMOBILIER
)
# Page Communications — envois courriel sans signature + audit.
api_router.include_router(
    immobilier_communications.router, dependencies=DEP_IMMOBILIER
)
# Relevés 31 (Revenu Québec) — suivi annuel par logement.
api_router.include_router(
    immobilier_releves31.router, dependencies=DEP_IMMOBILIER
)
# Documents personnalisés (règlement d'immeuble, contrat de chambreur…).
api_router.include_router(
    immobilier_docs_perso.router, dependencies=DEP_IMMOBILIER
)
# Assurances locataires — suivi annuel (page Suivis annuels).
api_router.include_router(
    immobilier_assurances.router, dependencies=DEP_IMMOBILIER
)
# Gestion externe : paiements par logement + factures ponctuelles.
api_router.include_router(
    immobilier_gestion_externe.router, dependencies=DEP_IMMOBILIER
)
api_router.include_router(
    immobilier_frais_gestion.router, dependencies=DEP_IMMOBILIER
)
# Exports CSV/Excel + zip de documents (/immobilier/exports/*,
# /immobilier/<sujet>/{id}/documents.zip) — avant immobilier.router.
api_router.include_router(
    immobilier_exports.router, dependencies=DEP_IMMOBILIER
)
# Validation bancaire des loyers (QuickBooks lecture seule) — avant
# immobilier.router (préfixe /immobilier/validation-bancaire).
api_router.include_router(
    immobilier_validation_bancaire.router, dependencies=DEP_IMMOBILIER
)
# Images immobilier : PAS de dépendance routeur — auth par ?t=<jwt> dans
# l'endpoint lui-même (les <img> ne portent pas de header Authorization).
api_router.include_router(immobilier.router_images)
api_router.include_router(contrats_gestion.router, dependencies=DEP_IMMOBILIER)
api_router.include_router(immobilier.router, dependencies=DEP_IMMOBILIER)
api_router.include_router(investissements.router, dependencies=DEP_INVESTISSEUR)
# Portail Investisseur v2 — participation par compagnie. La console
# admin (/invest/admin) est réservée admin/owner ; le portail
# (/invest/me) suit le volet investisseur.
api_router.include_router(
    invest_admin.router, dependencies=[Depends(get_current_admin_or_owner)]
)
api_router.include_router(invest_portal.router, dependencies=DEP_INVESTISSEUR)
# Formulaire public Dev Logiciel — endpoint POST /public/devlog/contact
# sans auth, cree un DevlogLead avec source=web_form au submit.
api_router.include_router(public_devlog_contact.router)

# Resume IA des notes de rencontre prospect (POST /devlog/leads/{id}/summarize-notes).
# Isole de devlog.py pour minimiser les conflits de merge entre chantiers.
# Protege par la meme garde admin/owner que les autres routers internes du pole.
api_router.include_router(devlog_notes_ai.router, dependencies=_devlog_admin_only)

# Vague 2 - endpoints projet (phases, taches, membres, finances). Tous
# nested sous /devlog/projects/{id}/* donc DOIVENT etre registered
# AVANT devlog.projects_router (deja registered plus haut). FastAPI
# match dans l'ordre d'enregistrement : on s'en sort parce que les
# paths nested portent un suffixe litteral (/phases, /tasks, ...) qui
# n'entre pas en collision avec le PATCH /devlog/projects/{item_id} du
# CRUD generique tant que {item_id} n'est pas suivi d'un segment.
api_router.include_router(
    devlog_project_phases.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_tasks.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_members.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_finances.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_photos.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_purchases.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_recap.router, dependencies=_devlog_admin_only
)
api_router.include_router(
    devlog_project_recurring_services.router, dependencies=_devlog_admin_only
)


