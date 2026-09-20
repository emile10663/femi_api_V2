"""
Point d'entrée du nouveau pipeline (Router + agents spécialisés).

Contrairement à FemiAgentManager (apps/femi_agent/agent/manager.py), ce
gestionnaire EXIGE un utilisateur résolu : ConversationState est un
OneToOneField vers Utilisateur, donc toute la logique de contexte
conversationnel (merge_context, context_resolved) n'a de sens que pour un
utilisateur identifié. Un numéro WhatsApp inconnu doit être géré EN AMONT,
dans apps/femi_whatsapp (webhook), avant d'appeler ce manager.

Portée actuelle (voir résumé de session, Point R4) : route_message/
agent (R4b-i/R4b-iii). ACCOUNTING (AccountingExecutor), FINANCIAL_ANALYST
(FinancialAnalystExecutor) et ACCOUNTING_MODIFY (AccountingModifyExecutor)
ont un exécuteur ; les autres agents (CUSTOMER, SETTINGS, UNKNOWN) sont
traités comme
needs_clarification=True avec un log explicite, jamais une exception.
operation_ids reste vide : l'écriture en base (R4b-ii) n'est pas encore
branchée.

entreprise est résolu une seule fois dans route_message()/aroute_message()
puis transmis explicitement à travers _build_result()/_dispatch_intent()
jusqu'à chaque exécuteur qui en a besoin (FinancialAnalystExecutor, plus
tard CustomerExecutor) — AccountingExecutor n'en a pas besoin (extraction
pure), mais le paramètre reste présent dans sa signature de dispatch pour
que _dispatch_intent() ait une signature uniforme, quel que soit l'agent.

Option B : ce fichier est séparé de manager.py pour que la coexistence
ancien/nouveau pipeline reste lisible (un fichier = un pipeline), et pour
simplifier le futur cutover (changer quel manager les vues Django
appellent, plutôt que trier des méthodes dans un fichier mixte).

Entrées multimédia (image_bytes/audio_bytes) : route_message()/
aroute_message() acceptent désormais ces deux paramètres optionnels, en
plus de message_text. Ils sont combinés en un seul texte AVANT le routage,
via _build_combined_text()/_abuild_combined_text() — RouterExecutor et tous
les agents en aval ne traitent jamais d'image ni d'audio directement, ils
ne connaissent que du texte (voir docstrings d'OCR_PROMPT/STT_PROMPT).
Ceci remplace, pour ce pipeline, le rôle que jouait
pipeline._extract_and_combine_text() pour l'ancien FemiAgentManager — mais
via les nouveaux agents OcrExecutor (OCR_PROMPT, vision) et SttExecutor
(STT_PROMPT, post-traitement texte), et non Tesseract/Whisper brut.
"""

import logging
from typing import Optional, Tuple

from asgiref.sync import sync_to_async

from apps.femi_account.models import Entreprise, Utilisateur
from apps.femi_agent.agent.accounting_executor import AccountingExecutor
from apps.femi_agent.agent.customer_executor import CustomerExecutor
from apps.femi_agent.agent.financial_analyst_executor import FinancialAnalystExecutor
from apps.femi_agent.agent.accounting_modify_executor import AccountingModifyExecutor
from apps.femi_agent.agent.manager import FemiAgentManager
from apps.femi_agent.agent.ocr_executor import OcrExecutor, OcrExecutionError
from apps.femi_agent.agent.stt_executor import SttExecutor, SttExecutionError
from apps.femi_agent.agent.router_executor import RouterExecutor, RouterExecutionError
from apps.femi_agent.parsers.audio_parser import transcribe_audio
from apps.femi_agent.schemas import (
    AccountingExtractionResult,
    AccountingModifyResult,
    AccountingModifyResolutionResult,
    AccountingModifyProposeChangeResult,
    AccountingModifySearchResult,
    FinalAnswerOutput,
    RouterIntent,
    CustomerExtractionOutput,
    RouterOutput,
    RouterProcessResult,
    ToolSelectionOutput,
    ValidationOutput,
)

logger = logging.getLogger(__name__)

# Type de retour possible d'un dispatch, selon l'agent — chaque agent a son
# propre schéma de résultat, jamais mélangés dans un seul objet.
DispatchResult = Optional[AccountingExtractionResult | ToolSelectionOutput | FinalAnswerOutput | AccountingModifyResult]


class RouterInputError(Exception):
    """Aucune donnée textuelle ou multimédia exploitable n'a été fournie
    (ni text_input, ni image_bytes, ni audio_bytes exploitables) —
    équivalent de AgentExecutionError levée par
    pipeline._extract_and_combine_text() pour l'ancien pipeline."""
    pass


class FemiRouterManager:
    """
    Gestionnaire central du nouveau pipeline (Router + agents spécialisés).

    Voir docstring de module pour la portée actuelle et la différence avec
    FemiAgentManager.
    """

    # --- POINTS D'ENTRÉE PUBLICS ---

    @classmethod
    async def aroute_message(
        cls,
        message_text: Optional[str] = None,
        entreprise_id: Optional[str] = None,
        utilisateur_id: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        audio_bytes: Optional[bytes] = None,
    ) -> RouterProcessResult:
        """Point d'entrée ASYNCHRONE : résout le tenant (strict), combine
        texte/OCR/audio, route, puis dispatche."""
        try:
            entreprise, utilisateur = await sync_to_async(cls._get_tenant_context_stricte)(
                entreprise_id, utilisateur_id
            )
            combined_text = await cls._abuild_combined_text(message_text, image_bytes, audio_bytes)
            router_output = await RouterExecutor.aexecute(combined_text)
            return await cls._abuild_result(router_output, entreprise)

        except (Entreprise.DoesNotExist, Utilisateur.DoesNotExist) as exc:
            logger.error("[FemiRouterManager] Contexte tenant introuvable (async): %s", exc)
            return RouterProcessResult(success=False, message=str(exc))
        except RouterInputError as exc:
            logger.warning("[FemiRouterManager] Aucune donnée exploitable (async): %s", exc)
            return RouterProcessResult(success=False, message=str(exc))
        except (OcrExecutionError, SttExecutionError, ValueError) as exc:
            logger.error("[FemiRouterManager] Échec OCR/audio (async): %s", exc)
            return RouterProcessResult(success=False, message="Le traitement de l'image ou de l'audio a échoué.")
        except RouterExecutionError as exc:
            logger.error("[FemiRouterManager] Échec du Router (async): %s", exc)
            return RouterProcessResult(success=False, message="Le routage du message a échoué.")
        except Exception:
            logger.exception("[FemiRouterManager] Erreur inattendue (async)")
            return RouterProcessResult(success=False, message="Une erreur technique est survenue.")

    @classmethod
    def route_message(
        cls,
        message_text: Optional[str] = None,
        entreprise_id: Optional[str] = None,
        utilisateur_id: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        audio_bytes: Optional[bytes] = None,
    ) -> RouterProcessResult:
        """Point d'entrée SYNCHRONE : résout le tenant (strict), combine
        texte/OCR/audio, route, puis dispatche."""
        try:
            entreprise, utilisateur = cls._get_tenant_context_stricte(entreprise_id, utilisateur_id)
            combined_text = cls._build_combined_text(message_text, image_bytes, audio_bytes)
            router_output = RouterExecutor.execute(combined_text)
            return cls._build_result(router_output, entreprise)

        except (Entreprise.DoesNotExist, Utilisateur.DoesNotExist) as exc:
            logger.error("[FemiRouterManager] Contexte tenant introuvable (sync): %s", exc)
            return RouterProcessResult(success=False, message=str(exc))
        except RouterInputError as exc:
            logger.warning("[FemiRouterManager] Aucune donnée exploitable (sync): %s", exc)
            return RouterProcessResult(success=False, message=str(exc))
        except (OcrExecutionError, SttExecutionError, ValueError) as exc:
            logger.error("[FemiRouterManager] Échec OCR/audio (sync): %s", exc)
            return RouterProcessResult(success=False, message="Le traitement de l'image ou de l'audio a échoué.")
        except RouterExecutionError as exc:
            logger.error("[FemiRouterManager] Échec du Router (sync): %s", exc)
            return RouterProcessResult(success=False, message="Le routage du message a échoué.")
        except Exception:
            logger.exception("[FemiRouterManager] Erreur inattendue (sync)")
            return RouterProcessResult(success=False, message="Une erreur technique est survenue.")

    # --- COMBINAISON TEXTE / OCR / AUDIO (mutualisée entre sync et async) ---

    @classmethod
    def _build_combined_text(
        cls,
        text_input: Optional[str],
        image_bytes: Optional[bytes],
        audio_bytes: Optional[bytes],
    ) -> str:
        """
        Combine texte utilisateur, OCR (OcrExecutor, OCR_PROMPT) et audio
        (transcribe_audio + SttExecutor, STT_PROMPT) en un seul texte,
        destiné à RouterExecutor. Seul texte_brut_complet (pas le JSON
        structuré) est utilisé pour l'OCR : RouterExecutor et les agents en
        aval ne traitent que du texte, jamais un schéma structuré en entrée.
        """
        parts: list[str] = []

        if text_input and text_input.strip():
            parts.append(text_input.strip())

        if image_bytes:
            ocr_result = OcrExecutor.execute(image_bytes)
            if ocr_result.texte_brut_complet:
                parts.append(ocr_result.texte_brut_complet)

        if audio_bytes:
            raw_transcription = transcribe_audio(audio_bytes)
            stt_result = SttExecutor.execute(raw_transcription)
            if stt_result.corrected_text:
                parts.append(stt_result.corrected_text)

        combined = "\n".join(parts).strip()
        if not combined:
            raise RouterInputError("Aucune donnée textuelle ou multimédia exploitable.")

        return combined

    @classmethod
    async def _abuild_combined_text(
        cls,
        text_input: Optional[str],
        image_bytes: Optional[bytes],
        audio_bytes: Optional[bytes],
    ) -> str:
        """Équivalent ASYNCHRONE de _build_combined_text()."""
        parts: list[str] = []

        if text_input and text_input.strip():
            parts.append(text_input.strip())

        if image_bytes:
            ocr_result = await OcrExecutor.aexecute(image_bytes)
            if ocr_result.texte_brut_complet:
                parts.append(ocr_result.texte_brut_complet)

        if audio_bytes:
            raw_transcription = await sync_to_async(transcribe_audio)(audio_bytes)
            stt_result = await SttExecutor.aexecute(raw_transcription)
            if stt_result.corrected_text:
                parts.append(stt_result.corrected_text)

        combined = "\n".join(parts).strip()
        if not combined:
            raise RouterInputError("Aucune donnée textuelle ou multimédia exploitable.")

        return combined

    # --- LOGIQUE PARTAGÉE (mutualisée entre sync et async) ---

    @classmethod
    def _get_tenant_context_stricte(
        cls, entreprise_id: Optional[str], utilisateur_id: Optional[str]
    ) -> Tuple[Entreprise, Utilisateur]:
        """
        Récupère entreprise + utilisateur, tous deux OBLIGATOIRES.

        Réutilise FemiAgentManager._get_entreprise() (déjà strict, pure,
        sans dépendance à l'ancien pipeline) plutôt que de le dupliquer.
        _get_utilisateur_stricte() est propre à ce manager : l'ancien
        _get_utilisateur() tolère utilisateur_id vide et retourne None
        silencieusement, ce qui est incompatible avec ConversationState
        (OneToOneField vers Utilisateur).
        """
        entreprise = FemiAgentManager._get_entreprise(entreprise_id)
        utilisateur = cls._get_utilisateur_stricte(utilisateur_id)
        return entreprise, utilisateur

    @classmethod
    def _get_utilisateur_stricte(cls, utilisateur_id: Optional[str]) -> Utilisateur:
        """Lève Utilisateur.DoesNotExist si non résolu (jamais de None silencieux)."""
        if utilisateur_id:
            utilisateur = Utilisateur.objects.filter(id=utilisateur_id).first()
            if utilisateur is not None:
                return utilisateur
        raise Utilisateur.DoesNotExist(
            f"Aucun utilisateur résolu pour utilisateur_id={utilisateur_id!r}."
        )

    # --- DISPATCH VERS LES AGENTS SPÉCIALISÉS (R4b-i / R4b-iii) ---

    @classmethod
    def _dispatch_intent(cls, intent: RouterIntent, entreprise: Entreprise) -> Tuple[DispatchResult, bool]:
        """
        Dispatch SYNCHRONE d'un intent vers l'exécuteur de son agent.

        Retourne (résultat de l'agent ou None, needs_clarification imposé
        par le dispatch). ACCOUNTING et FINANCIAL_ANALYST ont un exécuteur ;
        tout autre agent est traité comme needs_clarification=True avec un
        log explicite, jamais une exception ni un abandon silencieux.
        """
        if intent.agent == "ACCOUNTING":
            result = AccountingExecutor.execute(intent.raw_segment, entreprise)
            return result, result.needs_clarification

        if intent.agent == "FINANCIAL_ANALYST":
            result = FinancialAnalystExecutor.execute(intent.raw_segment, entreprise)
            return result, getattr(result, "needs_clarification", False)

        if intent.agent == "ACCOUNTING_MODIFY":
            result = AccountingModifyExecutor.execute(intent.raw_segment, entreprise)
            return result, result.needs_clarification

        if intent.agent == "CUSTOMER":
            result = CustomerExecutor.execute(intent.raw_segment, entreprise)
            return result, getattr(result, "needs_clarification", False)

        logger.warning(
            "[FemiRouterManager] Agent '%s' pas encore implémenté (intent_id=%s) — "
            "clarification demandée.",
            intent.agent,
            intent.intent_id,
        )
        return None, True

    @classmethod
    async def _adispatch_intent(
        cls, intent: RouterIntent, entreprise: Entreprise
    ) -> Tuple[DispatchResult, bool]:
        """Équivalent ASYNCHRONE de _dispatch_intent()."""
        if intent.agent == "ACCOUNTING":
            result = await AccountingExecutor.aexecute(intent.raw_segment, entreprise)
            return result, result.needs_clarification

        if intent.agent == "FINANCIAL_ANALYST":
            result = await FinancialAnalystExecutor.aexecute(intent.raw_segment, entreprise)
            return result, result.needs_clarification

        if intent.agent == "ACCOUNTING_MODIFY":
            result = await AccountingModifyExecutor.aexecute(intent.raw_segment, entreprise)
            return result, result.needs_clarification

        if intent.agent == "CUSTOMER":
            result = await CustomerExecutor.aexecute(intent.raw_segment, entreprise)
            return result, getattr(result, "needs_clarification", False)

        logger.warning(
            "[FemiRouterManager] Agent '%s' pas encore implémenté (intent_id=%s) — "
            "clarification demandée.",
            intent.agent,
            intent.intent_id,
        )
        return None, True

    @classmethod
    def _build_result(cls, router_output: RouterOutput, entreprise: Entreprise) -> RouterProcessResult:
        """Construit le RouterProcessResult en dispatchant chaque intent (SYNCHRONE)."""
        needs_clarification = False
        missing_fields: list[str] = []
        accounting_results: list[AccountingExtractionResult] = []
        financial_analyst_results: list[ToolSelectionOutput | FinalAnswerOutput] = []
        accounting_modify_results: list[AccountingModifySearchResult | AccountingModifyResolutionResult | AccountingModifyProposeChangeResult] = []
        customer_results: list[ValidationOutput | CustomerExtractionOutput | ToolSelectionOutput | FinalAnswerOutput] = []

        for intent in router_output.intents:
            if intent.needs_clarification:
                needs_clarification = True
            for field in intent.missing_fields:
                if field not in missing_fields:
                    missing_fields.append(field)

            result, dispatch_needs_clarification = cls._dispatch_intent(intent, entreprise)
            if dispatch_needs_clarification:
                needs_clarification = True

            if isinstance(result, AccountingExtractionResult):
                accounting_results.append(result)
            elif isinstance(result, (AccountingModifySearchResult, AccountingModifyResolutionResult, AccountingModifyProposeChangeResult)):
                accounting_modify_results.append(result)
            elif intent.agent == "CUSTOMER":
                customer_results.append(result)
            elif isinstance(result, (ToolSelectionOutput, FinalAnswerOutput)):
                financial_analyst_results.append(result)

            # missing_fields n'existe que sur AccountingExtractionResult et
            # ToolSelectionOutput (voir schemas/tool_loop.py) — jamais sur
            # FinalAnswerOutput, qui n'a pas ce champ.
            if result is not None and hasattr(result, "missing_fields"):
                for field in result.missing_fields:
                    if field not in missing_fields:
                        missing_fields.append(field)

        return RouterProcessResult(
            success=True,
            message="Message routé avec succès.",
            router_output=router_output,
            operation_ids=[],
            needs_clarification=needs_clarification,
            missing_fields=missing_fields,
            accounting_results=accounting_results,
            financial_analyst_results=financial_analyst_results,
            accounting_modify_results=accounting_modify_results,
            customer_results=customer_results,
        )

    @classmethod
    async def _abuild_result(cls, router_output: RouterOutput, entreprise: Entreprise) -> RouterProcessResult:
        """Construit le RouterProcessResult en dispatchant chaque intent (ASYNCHRONE)."""
        needs_clarification = False
        missing_fields: list[str] = []
        accounting_results: list[AccountingExtractionResult] = []
        financial_analyst_results: list[ToolSelectionOutput | FinalAnswerOutput] = []
        accounting_modify_results: list[AccountingModifySearchResult | AccountingModifyResolutionResult | AccountingModifyProposeChangeResult] = []
        customer_results: list[ValidationOutput | CustomerExtractionOutput | ToolSelectionOutput | FinalAnswerOutput] = []

        for intent in router_output.intents:
            if intent.needs_clarification:
                needs_clarification = True
            for field in intent.missing_fields:
                if field not in missing_fields:
                    missing_fields.append(field)

            result, dispatch_needs_clarification = await cls._adispatch_intent(intent, entreprise)
            if dispatch_needs_clarification:
                needs_clarification = True

            if isinstance(result, AccountingExtractionResult):
                accounting_results.append(result)
            elif isinstance(result, (AccountingModifySearchResult, AccountingModifyResolutionResult, AccountingModifyProposeChangeResult)):
                accounting_modify_results.append(result)
            elif intent.agent == "CUSTOMER":
                customer_results.append(result)
            elif isinstance(result, (ToolSelectionOutput, FinalAnswerOutput)):
                financial_analyst_results.append(result)

            if result is not None and hasattr(result, "missing_fields"):
                for field in result.missing_fields:
                    if field not in missing_fields:
                        missing_fields.append(field)

        return RouterProcessResult(
            success=True,
            message="Message routé avec succès.",
            router_output=router_output,
            operation_ids=[],
            needs_clarification=needs_clarification,
            missing_fields=missing_fields,
            accounting_results=accounting_results,
            financial_analyst_results=financial_analyst_results,
            accounting_modify_results=accounting_modify_results,
            customer_results=customer_results,
        )
