"""Vocabulaire de périodes partagé (extrait de financials.py).

PERIODES_VALIDES et resolve_periode() sont le SEUL point de définition du
vocabulaire de périodes du prompt (aujourd'hui, hier, cette_semaine,
semaine_derniere, ce_mois, mois_dernier, cette_annee, annee_derniere,
depuis_debut, personnalisee + date_debut/date_fin) — règle "un seul
vocabulaire de périodes", jamais réinventé par module.

Utilisé par financials.py (_ops_periode()) et par
tools/operations.py (find_matching_operations(), filtre date_range).

_ops_periode() (filtre de QuerySet Operation propre à financials.py) reste
dans financials.py : operations.py construit son propre queryset avec
davantage de critères (contact_nom, amount_ttc, category,
description_keywords...), pas de réutilisation directe possible.
"""

import calendar
from datetime import timedelta

from django.utils import timezone

PERIODES_VALIDES = {
    "aujourd'hui", "hier", "cette_semaine", "semaine_derniere",
    "ce_mois", "mois_dernier", "cette_annee", "annee_derniere",
    "depuis_debut", "personnalisee",
}


def resolve_periode(periode, date_debut=None, date_fin=None, now=None):
    """
    Traduit une valeur de periode (vocabulaire du prompt) en bornes de dates.

    Returns:
        (start_date, end_date) : objets date, ou (None, None) pour depuis_debut
        (= aucun filtre de date).

    Raises:
        ValueError si periode est inconnue, ou si periode == "personnalisee"
        sans date_debut/date_fin.
    """
    if now is None:
        now = timezone.now()
    today = now.date()

    if periode not in PERIODES_VALIDES:
        raise ValueError(f"periode inconnue : {periode!r}")

    if periode == "aujourd'hui":
        return today, today

    if periode == "hier":
        hier = today - timedelta(days=1)
        return hier, hier

    if periode == "cette_semaine":
        lundi = today - timedelta(days=today.weekday())  # weekday() : lundi=0
        return lundi, today

    if periode == "semaine_derniere":
        lundi_cette_semaine = today - timedelta(days=today.weekday())
        lundi_derniere = lundi_cette_semaine - timedelta(days=7)
        dimanche_derniere = lundi_derniere + timedelta(days=6)
        return lundi_derniere, dimanche_derniere

    if periode == "ce_mois":
        debut = today.replace(day=1)
        return debut, today

    if periode == "mois_dernier":
        mois = 12 if today.month == 1 else today.month - 1
        annee = today.year - 1 if today.month == 1 else today.year
        debut = today.replace(year=annee, month=mois, day=1)
        dernier_jour = calendar.monthrange(annee, mois)[1]
        fin = debut.replace(day=dernier_jour)
        return debut, fin

    if periode == "cette_annee":
        return today.replace(month=1, day=1), today

    if periode == "annee_derniere":
        annee = today.year - 1
        return today.replace(year=annee, month=1, day=1), today.replace(year=annee, month=12, day=31)

    if periode == "depuis_debut":
        return None, None

    if periode == "personnalisee":
        if not date_debut or not date_fin:
            raise ValueError("periode='personnalisee' nécessite date_debut et date_fin")
        return date_debut, date_fin

    raise ValueError(f"periode non gérée : {periode!r}")  # filet de sécurité, ne devrait jamais arriver
