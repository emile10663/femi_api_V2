"""Exécuteur générique pour les prompts spécialisés du nouveau système
multi-agent (ACCOUNTING, FINANCIAL_ANALYST, CUSTOMER, OCR).

Factorise ce qui est commun à RouterExecutor (with_structured_output() en
chemin principal, fallback JSON manuel, gestion d'erreurs) pour éviter de
dupliquer cette logique dans chaque exécuteur d'agent.

Convention : chaque agent construit son propre prompt_text en amont
(injection de ses variables spécifiques via .replace(), jamais via
PromptTemplate/.format() à cause des accolades JSON littérales des
exemples) puis délègue l'appel LLM + parsing à cette classe.

Support multimodal (images) : ajouté pour OcrExecutor (OCR_PROMPT, agent
vision). Quand `images` est fourni (liste de chaînes base64, sans préfixe
data URI), le message "user" est construit au format multimodal attendu par
langchain_ollama (content = liste de blocs {"type": "text"/"image_url"}) —
voir langchain_ollama/chat_models.py, _convert_messages_to_ollama_messages.
Quand `images` est None (comportement historique, tous les autres agents),
le message "user" reste une simple chaîne de texte, inchangé.
"""

import json
import logging
import re

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ValidationError

from apps.femi_agent.agent.llm import get_llm

logger = logging.getLogger(__name__)

LOG_TEXT_PREVIEW_LEN = 80

_FALLBACK_FORMAT_SUFFIX = (
    "\n\n### FORMAT DE SORTIE (STRICT)\n"
    "Réponds uniquement avec un JSON valide, sans texte autour :\n"
    "{format_instructions}\n"
)


class BaseAgentExecutionError(Exception):
    """Exception générique pour un échec d'exécution d'un agent LLM structuré."""
    pass


class StructuredLLMExecutor:
    """
    Exécuteur générique : appelle le LLM avec sortie structurée contrainte
    à un schéma Pydantic donné, avec fallback JSON manuel en secours.
    Ne connaît ni le contenu du prompt ni le domaine métier de l'agent.
    """

    @staticmethod
    def _build_user_content(message_text: str, images: list[str] | None):
        """Construit le contenu du message "user" : texte simple si aucune
        image, sinon liste multimodale (texte + images en base64)."""
        if not images:
            return message_text
        content_parts: list[dict] = [{"type": "text", "text": message_text}]
        for image_b64 in images:
            content_parts.append({
                "type": "image_url",
                "image_url": f"data:image/jpeg;base64,{image_b64}",
            })
        return content_parts

    @staticmethod
    def execute(
        prompt_text: str,
        message_text: str,
        output_schema: type[BaseModel],
        num_ctx: int,
        error_cls: type[Exception] = BaseAgentExecutionError,
        log_prefix: str = "StructuredLLMExecutor",
        images: list[str] | None = None,
        model_name: str | None = None,
    ) -> BaseModel:
        """Exécution synchrone générique.

        Args:
            images: Liste optionnelle d'images en base64 (sans préfixe data
                URI). Si fourni, le message "user" est construit au format
                multimodal. Optionnel — absent pour tous les agents texte.
            model_name: Nom du modèle Ollama à utiliser, transmis à
                get_llm(). Optionnel — si absent, get_llm() applique son
                propre repli (settings.FEMI_LLM_MODEL).
        """
        try:
            llm = get_llm(model_name=model_name, num_ctx=num_ctx)
            structured_llm = llm.with_structured_output(output_schema)
            logger.debug(
                "[%s] Appel pour : '%s%s'",
                log_prefix,
                message_text[:LOG_TEXT_PREVIEW_LEN],
                "..." if len(message_text) > LOG_TEXT_PREVIEW_LEN else "",
            )
            messages = [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": StructuredLLMExecutor._build_user_content(message_text, images)},
            ]
            return structured_llm.invoke(messages)

        except (OutputParserException, ValidationError) as parse_err:
            logger.warning(
                "[%s] Structured output invalide, passage au fallback JSON manuel : %s",
                log_prefix, parse_err,
            )
            return StructuredLLMExecutor._fallback_parse(
                prompt_text, message_text, output_schema, num_ctx, error_cls, log_prefix, images, model_name
            )
        except Exception as e:
            logger.exception("[%s] Échec critique", log_prefix)
            raise error_cls(f"Erreur d'exécution ({log_prefix}) : {e}") from e

    @staticmethod
    async def aexecute(
        prompt_text: str,
        message_text: str,
        output_schema: type[BaseModel],
        num_ctx: int,
        error_cls: type[Exception] = BaseAgentExecutionError,
        log_prefix: str = "StructuredLLMExecutor",
        images: list[str] | None = None,
        model_name: str | None = None,
    ) -> BaseModel:
        """Exécution asynchrone générique (mêmes arguments que execute())."""
        try:
            llm = get_llm(model_name=model_name, num_ctx=num_ctx)
            structured_llm = llm.with_structured_output(output_schema)
            logger.debug(
                "[%s] Appel async pour : '%s%s'",
                log_prefix,
                message_text[:LOG_TEXT_PREVIEW_LEN],
                "..." if len(message_text) > LOG_TEXT_PREVIEW_LEN else "",
            )
            messages = [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": StructuredLLMExecutor._build_user_content(message_text, images)},
            ]
            return await structured_llm.ainvoke(messages)

        except (OutputParserException, ValidationError) as parse_err:
            logger.warning(
                "[%s] Structured output invalide (async), passage au fallback JSON manuel : %s",
                log_prefix, parse_err,
            )
            return StructuredLLMExecutor._fallback_parse(
                prompt_text, message_text, output_schema, num_ctx, error_cls, log_prefix, images, model_name
            )
        except Exception as e:
            logger.exception("[%s] Échec critique asynchrone", log_prefix)
            raise error_cls(f"Erreur d'exécution asynchrone ({log_prefix}) : {e}") from e

    @staticmethod
    def _fallback_parse(
        prompt_text: str,
        message_text: str,
        output_schema: type[BaseModel],
        num_ctx: int,
        error_cls: type[Exception],
        log_prefix: str,
        images: list[str] | None = None,
        model_name: str | None = None,
    ) -> BaseModel:
        """Secours : parsing JSON manuel si with_structured_output() échoue."""
        try:
            llm = get_llm(model_name=model_name, num_ctx=num_ctx)
            parser = PydanticOutputParser(pydantic_object=output_schema)
            full_prompt = prompt_text + _FALLBACK_FORMAT_SUFFIX.format(
                format_instructions=parser.get_format_instructions()
            )
            messages = [
                {"role": "system", "content": full_prompt},
                {"role": "user", "content": StructuredLLMExecutor._build_user_content(message_text, images)},
            ]
            raw_response = llm.invoke(messages)
            content = raw_response.content if hasattr(raw_response, "content") else str(raw_response)

            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                return output_schema(**data)
        except Exception:
            logger.exception("[%s] Le fallback JSON manuel a échoué", log_prefix)

        raise error_cls(
            f"Impossible d'obtenir une sortie valide ({log_prefix}) pour le message "
            f"('{message_text[:LOG_TEXT_PREVIEW_LEN]}...')"
        )
