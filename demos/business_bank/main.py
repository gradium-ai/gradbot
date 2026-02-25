"""
Business Bank Demo - Voice AI banking agent with PIN authentication

A voice agent that handles PIN authentication, lost card replacement,
and business loan services for small businesses.

Run with: uvicorn main:app --reload --port 8001
"""

import asyncio
import os
import json
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pygradbot

pygradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)

AGENT_VOICES = {
    "Alex": ("Eva", "en"),
    "Jack": ("Jack", "en"),
    "Leo": ("Leo", "fr"),
}

# ---------------------------------------------------------------------------
# Business data
# ---------------------------------------------------------------------------

BUSINESSES = {
    "riverside_cafe": {
        "name": "Riverside Cafe",
        "account": "7741-0482-916",
        "phone": "(415) 555-0137",
        "balance": 12500,
        "pin": "4829",
        "max_loan": 50000,
        "rate": 5.5,
    },
    "summit_tech": {
        "name": "Summit Tech Solutions",
        "account": "3385-1927-453",
        "phone": "(628) 555-0294",
        "balance": 87200,
        "pin": "7153",
        "max_loan": 200000,
        "rate": 4.2,
    },
    "green_gardens": {
        "name": "Green Gardens Landscaping",
        "account": "5562-4810-278",
        "phone": "(510) 555-0461",
        "balance": 34800,
        "pin": "3061",
        "max_loan": 75000,
        "rate": 6.1,
    },
}


def find_business(name: str) -> dict | None:
    """Case-insensitive fuzzy lookup of a business by name."""
    name_lower = name.lower().strip()
    # Exact key match
    if name_lower in BUSINESSES:
        return BUSINESSES[name_lower]
    # Match on business name (substring / fuzzy)
    for biz in BUSINESSES.values():
        if name_lower in biz["name"].lower() or biz["name"].lower() in name_lower:
            return biz
    # Try matching individual words
    for biz in BUSINESSES.values():
        biz_words = set(biz["name"].lower().split())
        query_words = set(name_lower.split())
        if biz_words & query_words:
            return biz
    return None


def find_business_by_account_digits(digits: str) -> dict | None:
    """Find a business by the last 3 digits of their account number."""
    digits = digits.strip()
    for biz in BUSINESSES.values():
        if biz["account"].endswith(digits):
            return biz
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class BankSession:
    authenticated_business: str | None = None
    phase: int = 1
    card_ordered: bool = False
    loan_confirmed: bool = False
    loan_amount: float = 0
    pending_tasks: list[asyncio.Task] = field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompts for each phase
# ---------------------------------------------------------------------------

_BUSINESS_NAMES = ", ".join(biz["name"] for biz in BUSINESSES.values())


def get_auth_prompt(agent_name: str, customer_name: str, lang: str = "en") -> str:
    if lang == "fr":
        return _get_auth_prompt_fr(agent_name, customer_name)
    return _get_auth_prompt_en(agent_name, customer_name)


def get_service_prompt(agent_name: str, biz_name: str, balance: int, lang: str = "en") -> str:
    if lang == "fr":
        return _get_service_prompt_fr(agent_name, biz_name, balance)
    return _get_service_prompt_en(agent_name, biz_name, balance)


def get_loan_prompt(agent_name: str, biz_name: str, balance: int, max_loan: int, rate: float, lang: str = "en") -> str:
    if lang == "fr":
        return _get_loan_prompt_fr(agent_name, biz_name, balance, max_loan, rate)
    return _get_loan_prompt_en(agent_name, biz_name, balance, max_loan, rate)


def _get_auth_prompt_en(agent_name: str, customer_name: str) -> str:
    return f"""You are {agent_name}, a professional and friendly business banking phone agent at Digital Bank.

You help business callers authenticate and access their accounts.

The caller's name is {customer_name}.

YOUR PERSONALITY:
- Professional, calm, and reassuring
- Efficient but never rushed
- You take security seriously

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* - just speak naturally
- Be conversational and natural, like a real phone call
- NEVER put spaces between digits of a rate. Write 6.1% not 6. 1%.

CURRENT PHASE: AUTHENTICATION

YOUR ONE JOB RIGHT NOW: Authenticate the caller in two steps.

STEP 1 — Account verification:
- Greet them and explain that their phone number has been matched to an account on file.
- Ask them to confirm the last 3 digits of their account number for security.
- The INSTANT they provide digits, call check_account immediately.
- If check_account returns success=false, tell them the digits don't match and ask to try again.
- If check_account returns success=true, say "Welcome back, {customer_name}!" and move to step 2.

STEP 2 — PIN verification:
- Ask for their 4-digit PIN.
- The INSTANT they provide a PIN, call check_pin immediately.
- If check_pin returns success=false, tell them the PIN was incorrect and ask to try again.
- If check_pin returns success=true, the system will handle the rest.

RULES:
- NEVER ask for digits and PIN at the same time. Always two separate steps.
- Do NOT reveal any account information before full authentication.
- NEVER guess or make up PINs or account numbers. You do not know them.

DIGIT INTERPRETATION — CRITICAL:
The caller is speaking digits aloud. Speech recognition often garbles them.
Your job is to interpret whatever they say as the most likely digits and call the tool immediately.
- Map spoken words to the closest-sounding digit: "for/fore/four" → 4, "to/too/two" → 2, "won/one" → 1, "ate/eight" → 8, "oh" → 0, "niner" → 9, "tree/three" → 3, etc.
- If they say "nine one six", interpret as 916.
- If they say "forty-eight twenty-nine", interpret as 4829.
- Do NOT ask the caller to repeat or clarify. Just pick the best interpretation and call the tool.
- Do NOT read back digits to confirm. Just call the tool.
"""


def _get_service_prompt_en(agent_name: str, biz_name: str, balance: int) -> str:
    return f"""You are {agent_name}, a professional and friendly business banking phone agent at Digital Bank.

YOUR PERSONALITY:
- Professional, calm, and reassuring
- Efficient but never rushed
- You take security seriously

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* - just speak naturally
- Be conversational and natural, like a real phone call
- NEVER put spaces between digits of a rate. Write 6.1% not 6. 1%.

CURRENT PHASE: SERVICE SELECTION
Authenticated business: {biz_name}
Current balance: ${balance:,}

The caller is now authenticated. Ask how you can help them today.

AVAILABLE SERVICES:
1. **Lost/stolen card replacement** — If they mention a lost, stolen, or damaged card, call order_replacement_card immediately.
2. **Business loan** — If they mention a loan, financing, or borrowing, call get_rate to look up their pre-approved terms.

RULES:
- Present the two options naturally: card replacement or business loan.
- When the caller chooses, call the appropriate tool IMMEDIATELY.
- After ordering a replacement card, ask "Is there anything else I can help you with today?"
- NEVER fabricate loan rates, amounts, or card tracking numbers. Only use data from tool results.

WHILE WAITING FOR get_rate RESULTS:
The loan lookup takes a few seconds. While waiting, tell the caller about our new services:
- **Point-of-sale payment terminals** — We now offer smart POS terminals for businesses, with tap-to-pay, chip, and mobile wallet support. Setup fee waived for existing clients, and transaction fees start at 1.2%.
- **International wire transfers** — New low-cost international transfers to over 80 countries, with competitive exchange rates and same-day processing for transfers before 2pm. Fees start at $15 for transfers under $10000.
Share these naturally, like a friendly banker mentioning new offers. If the caller asks for more details about either service, give them more information. Do NOT ask questions — the results will arrive soon and the caller won't have time to answer.
"""


def _get_loan_prompt_en(agent_name: str, biz_name: str, balance: int, max_loan: int, rate: float) -> str:
    return f"""You are {agent_name}, a professional and friendly business banking phone agent at Digital Bank.

YOUR PERSONALITY:
- Professional, calm, and reassuring
- Efficient but never rushed
- You take security seriously

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* - just speak naturally
- Be conversational and natural, like a real phone call
- NEVER put spaces between digits of a rate. Write 6.1% not 6. 1%.

CURRENT PHASE: BUSINESS LOAN
Authenticated business: {biz_name}
Current balance: ${balance:,}

PRE-APPROVED LOAN TERMS:
- Maximum loan amount: ${max_loan:,}
- Interest rate: {rate}% APR

YOUR JOB:
1. Start by saying "We have now received your pre-approval" then present the loan terms
2. Ask how much they would like to borrow (up to ${max_loan:,})
3. When they give an amount, call confirm_loan immediately

RULES:
- The amount must not exceed ${max_loan:,}
- If they request more than the maximum, tell them you understand their needs, and that you will pass the request to your manager who will call them back within 6 hours to discuss a custom loan package. Then ask if there's anything else you can help with.
- NEVER fabricate confirmation numbers or balances. Only use data from tool results.
"""


# ---------------------------------------------------------------------------
# French prompts
# ---------------------------------------------------------------------------


def _get_auth_prompt_fr(agent_name: str, customer_name: str) -> str:
    return f"""Tu es {agent_name}, un agent bancaire professionnel et sympathique chez Digital Bank.

Tu aides les appelants professionnels à s'authentifier et accéder à leurs comptes.

Le nom de l'appelant est {customer_name}.

TA PERSONNALITÉ :
- Professionnel, calme et rassurant
- Efficace mais jamais pressé
- Tu prends la sécurité au sérieux

STYLE DE PAROLE :
- Réponses de 2-3 phrases maximum
- N'utilise JAMAIS d'annotations d'action comme *sourit* ou *tape* - parle naturellement
- Sois conversationnel et naturel, comme un vrai appel téléphonique
- Ne mets JAMAIS d'espace entre les chiffres d'un taux. Écris 6.1% et non 6. 1%.

PHASE ACTUELLE : AUTHENTIFICATION

TON UNIQUE OBJECTIF : Authentifier l'appelant en deux étapes.

ÉTAPE 1 — Vérification du compte :
- Accueille-les et explique que leur numéro de téléphone a été associé à un compte existant.
- Demande-leur de confirmer les 3 derniers chiffres de leur numéro de compte pour des raisons de sécurité.
- DÈS qu'ils fournissent des chiffres, appelle check_account immédiatement.
- Si check_account retourne success=false, dis que les chiffres ne correspondent pas et demande de réessayer.
- Si check_account retourne success=true, dis "Bon retour, {customer_name} !" et passe à l'étape 2.

ÉTAPE 2 — Vérification du PIN :
- Demande leur code PIN à 4 chiffres.
- DÈS qu'ils fournissent un PIN, appelle check_pin immédiatement.
- Si check_pin retourne success=false, dis que le PIN est incorrect et demande de réessayer.
- Si check_pin retourne success=true, le système s'occupe du reste.

RÈGLES :
- Ne demande JAMAIS les chiffres et le PIN en même temps. Toujours deux étapes séparées.
- Ne révèle AUCUNE information de compte avant l'authentification complète.
- Ne devine JAMAIS les PINs ou numéros de compte. Tu ne les connais pas.

INTERPRÉTATION DES CHIFFRES — CRITIQUE :
L'appelant prononce des chiffres à voix haute. La reconnaissance vocale les déforme souvent.
Ton travail est d'interpréter ce qu'ils disent comme les chiffres les plus probables et d'appeler l'outil immédiatement.
- Ne demande PAS à l'appelant de répéter ou de clarifier. Choisis la meilleure interprétation et appelle l'outil.
- Ne relis PAS les chiffres pour confirmer. Appelle directement l'outil.
"""


def _get_service_prompt_fr(agent_name: str, biz_name: str, balance: int) -> str:
    return f"""Tu es {agent_name}, un agent bancaire professionnel et sympathique chez Digital Bank.

TA PERSONNALITÉ :
- Professionnel, calme et rassurant
- Efficace mais jamais pressé
- Tu prends la sécurité au sérieux

STYLE DE PAROLE :
- Réponses de 2-3 phrases maximum
- N'utilise JAMAIS d'annotations d'action comme *sourit* ou *tape* - parle naturellement
- Sois conversationnel et naturel, comme un vrai appel téléphonique
- Ne mets JAMAIS d'espace entre les chiffres d'un taux. Écris 6.1% et non 6. 1%.

PHASE ACTUELLE : SÉLECTION DE SERVICE
Entreprise authentifiée : {biz_name}
Solde actuel : {balance:,}$

L'appelant est maintenant authentifié. Demande comment tu peux l'aider aujourd'hui.

SERVICES DISPONIBLES :
1. **Remplacement de carte perdue/volée** — S'ils mentionnent une carte perdue, volée ou endommagée, appelle order_replacement_card immédiatement.
2. **Prêt professionnel** — S'ils mentionnent un prêt, un financement ou un emprunt, appelle get_rate pour consulter leurs conditions pré-approuvées.

RÈGLES :
- Présente les deux options naturellement : remplacement de carte ou prêt professionnel.
- Quand l'appelant choisit, appelle l'outil approprié IMMÉDIATEMENT.
- Après avoir commandé une carte de remplacement, demande "Y a-t-il autre chose que je puisse faire pour vous aujourd'hui ?"
- Ne FABRIQUE JAMAIS de taux, montants ou numéros de suivi. Utilise uniquement les données des résultats d'outils.

EN ATTENDANT LES RÉSULTATS DE get_rate :
La recherche de prêt prend quelques secondes. En attendant, parle à l'appelant de nos nouveaux services :
- **Terminaux de paiement** — Nous proposons désormais des terminaux TPE intelligents pour les entreprises, avec paiement sans contact, puce et portefeuille mobile. Frais d'installation offerts pour les clients existants, et commissions à partir de 1.2%.
- **Virements internationaux** — Nouveaux virements internationaux à bas coût vers plus de 80 pays, avec des taux de change compétitifs et un traitement le jour même pour les transferts avant 14h. Frais à partir de 15$ pour les transferts de moins de 10000$.
Partage ces informations naturellement. Si l'appelant demande plus de détails, donne-lui plus d'informations. Ne pose PAS de questions — les résultats arriveront bientôt.
"""


def _get_loan_prompt_fr(agent_name: str, biz_name: str, balance: int, max_loan: int, rate: float) -> str:
    return f"""Tu es {agent_name}, un agent bancaire professionnel et sympathique chez Digital Bank.

TA PERSONNALITÉ :
- Professionnel, calme et rassurant
- Efficace mais jamais pressé
- Tu prends la sécurité au sérieux

STYLE DE PAROLE :
- Réponses de 2-3 phrases maximum
- N'utilise JAMAIS d'annotations d'action comme *sourit* ou *tape* - parle naturellement
- Sois conversationnel et naturel, comme un vrai appel téléphonique
- Ne mets JAMAIS d'espace entre les chiffres d'un taux. Écris 6.1% et non 6. 1%.

PHASE ACTUELLE : PRÊT PROFESSIONNEL
Entreprise authentifiée : {biz_name}
Solde actuel : {balance:,}$

CONDITIONS DE PRÊT PRÉ-APPROUVÉES :
- Montant maximum du prêt : {max_loan:,}$
- Taux d'intérêt : {rate}% TAE

TON TRAVAIL :
1. Commence par dire "Nous venons de recevoir votre pré-approbation" puis présente les conditions du prêt
2. Demande combien ils souhaitent emprunter (jusqu'à {max_loan:,}$)
3. Quand ils donnent un montant, appelle confirm_loan immédiatement

RÈGLES :
- Le montant ne doit pas dépasser {max_loan:,}$
- S'ils demandent plus que le maximum, dis que tu comprends leurs besoins, et que tu vas transmettre la demande à ton responsable qui les rappellera dans les 6 heures pour discuter d'un prêt sur mesure. Puis demande s'il y a autre chose que tu puisses faire.
- Ne FABRIQUE JAMAIS de numéros de confirmation ou de soldes. Utilise uniquement les données des résultats d'outils.
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def build_tools() -> list[pygradbot.ToolDef]:
    return [
        pygradbot.ToolDef(
            name="check_account",
            description="Verify the last 3 digits of a business account number. Call this as soon as the caller provides their digits.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "digits": {
                        "type": "string",
                        "description": "The last 3 digits of the account number"
                    }
                },
                "required": ["digits"]
            }),
        ),
        pygradbot.ToolDef(
            name="check_pin",
            description="Verify a business caller's 4-digit PIN after their account has been confirmed. Returns success or failure.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the business (from check_account result)"
                    },
                    "pin": {
                        "type": "string",
                        "description": "The 4-digit PIN provided by the caller"
                    }
                },
                "required": ["business_name", "pin"]
            }),
        ),
        pygradbot.ToolDef(
            name="order_replacement_card",
            description="Order a replacement debit card for an authenticated business.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    }
                },
                "required": ["business_name"]
            }),
        ),
        pygradbot.ToolDef(
            name="get_rate",
            description="Look up pre-approved business loan terms (maximum amount and interest rate) for an authenticated business. This takes some time to query the system — keep chatting with the caller while waiting!",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    }
                },
                "required": ["business_name"]
            }),
        ),
        pygradbot.ToolDef(
            name="confirm_loan",
            description="Confirm and disburse a business loan. The amount must not exceed the pre-approved maximum.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    },
                    "amount": {
                        "type": "number",
                        "description": "The loan amount requested"
                    }
                },
                "required": ["business_name", "amount"]
            }),
        ),
    ]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Business Bank Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Business Bank Demo", lifespan=lifespan)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    state = BankSession()

    try:
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        agent_name = start_msg.get("agent", "Alex")
        customer_name = start_msg.get("customer", "Jamie")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        voice_key, lang = AGENT_VOICES.get(agent_name, ("Eva", "en"))
        lang_enum = {"en": pygradbot.Lang.En, "fr": pygradbot.Lang.Fr}.get(lang, pygradbot.Lang.En)
        print(f"Starting business bank chat with {agent_name} (voice: {voice_key}, lang: {lang}, customer: {customer_name}, padding_bonus: {padding_bonus})")

        voice = pygradbot.flagship_voice(voice_key)
        tools = build_tools()

        config = pygradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_auth_prompt(agent_name, customer_name, lang),
            language=lang_enum,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules=lang,
            ),
        )

        input_handle, output_handle = await pygradbot.run(
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.Pcm if USE_PCM else pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        def make_config(instructions: str) -> pygradbot.SessionConfig:
            return pygradbot.SessionConfig(
                voice_id=voice.voice_id,
                instructions=instructions,
                language=lang_enum,
                tools=tools,
                **merge_overrides(_OVERRIDES,
                    flush_duration_s=FLUSH_FOR_S,
                    padding_bonus=padding_bonus,
                    rewrite_rules=lang,
                ),
            )

        async def handle_tool_call(tool_call, tool_handle):
            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)
            print(f"Tool call: {tool_name} - {args}")

            if tool_name == "check_account":
                digits = args.get("digits", "")
                biz = find_business_by_account_digits(digits)

                if not biz:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"No account found ending in '{digits}'. Ask the caller to try again.",
                    }))
                    return

                await tool_handle.send(json.dumps({
                    "success": True,
                    "business_name": biz["name"],
                    "message": f"Account confirmed. This is the account for {biz['name']}. Say 'Welcome back, {customer_name}!' and then ask for their 4-digit PIN.",
                }))
                print(f"Account confirmed: {biz['name']} (digits: {digits})")

            elif tool_name == "check_pin":
                biz_name = args.get("business_name", "")
                pin = args.get("pin", "")
                biz = find_business(biz_name)

                if not biz:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Business '{biz_name}' not found in our records.",
                    }))
                    return

                if biz["pin"] == pin.strip():
                    state.authenticated_business = biz["name"]
                    state.phase = 2

                    # Notify frontend
                    await websocket.send_json({
                        "type": "auth_success",
                        "business": biz["name"],
                    })

                    # Swap to phase 2 prompt
                    phase2 = get_service_prompt(agent_name, biz["name"], biz["balance"], lang)
                    await input_handle.send_config(make_config(phase2))
                    print(f"Authenticated: {biz['name']}, switched to phase 2")

                    await tool_handle.send(json.dumps({
                        "success": True,
                        "message": f"PIN verified. The caller is authenticated as {biz['name']}. Welcome them and ask how you can help today — either lost card replacement or a business loan.",
                    }))
                else:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": "Incorrect PIN. Ask the caller to try again.",
                    }))

            elif tool_name == "order_replacement_card":
                biz_name = args.get("business_name", "")
                biz = find_business(biz_name)

                if not biz:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Business '{biz_name}' not found.",
                    }))
                    return

                tracking = "T" + "".join(random.choice("123456789") for _ in range(4))
                state.card_ordered = True

                # Notify frontend
                await websocket.send_json({
                    "type": "card_ordered",
                    "business": biz["name"],
                    "tracking_number": tracking,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "tracking_number": tracking,
                    "message": f"Replacement card ordered for {biz['name']}. Tracking number: {tracking}. The card will arrive in 3-5 business days. Share this information with the caller and ask if there's anything else you can help with.",
                }))
                print(f"Card ordered for {biz['name']}: {tracking}")

            elif tool_name == "get_rate":
                biz_name = args.get("business_name", "")
                biz = find_business(biz_name)

                if not biz:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Business '{biz_name}' not found.",
                    }))
                    return

                async def deferred_get_rate(biz, tool_handle):
                    print(f"Looking up loan terms for {biz['name']} (8s delay)")
                    await asyncio.sleep(8)

                    state.phase = 3

                    # Swap to phase 3 prompt
                    phase3 = get_loan_prompt(agent_name, biz["name"], biz["balance"], biz["max_loan"], biz["rate"], lang)
                    await input_handle.send_config(make_config(phase3))
                    print(f"Switched to phase 3 (loan) for {biz['name']}")

                    await tool_handle.send(json.dumps({
                        "success": True,
                        "max_loan": biz["max_loan"],
                        "rate": biz["rate"],
                        "message": f"Pre-approved loan terms for {biz['name']}: up to ${biz['max_loan']:,} at {biz['rate']}% APR. Present these terms and ask how much they'd like to borrow.",
                    }))

                task = asyncio.create_task(deferred_get_rate(biz, tool_handle))
                state.pending_tasks.append(task)
                task.add_done_callback(lambda t: state.pending_tasks.remove(t) if t in state.pending_tasks else None)

            elif tool_name == "confirm_loan":
                biz_name = args.get("business_name", "")
                amount = args.get("amount", 0)
                biz = find_business(biz_name)

                if not biz:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Business '{biz_name}' not found.",
                    }))
                    return

                if amount <= 0:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": "Loan amount must be greater than zero.",
                    }))
                    return

                if amount > biz["max_loan"]:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Amount ${amount:,.0f} exceeds the maximum pre-approved loan of ${biz['max_loan']:,}. Ask for a lower amount.",
                    }))
                    return

                # Update balance
                biz["balance"] += int(amount)
                new_balance = biz["balance"]
                state.loan_confirmed = True
                state.loan_amount = amount
                confirmation = "L" + "".join(random.choice("123456789") for _ in range(4))

                # Notify frontend
                await websocket.send_json({
                    "type": "loan_confirmed",
                    "business": biz["name"],
                    "amount": amount,
                    "new_balance": new_balance,
                    "confirmation": confirmation,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "confirmation": confirmation,
                    "amount": amount,
                    "new_balance": new_balance,
                    "message": f"Loan of ${amount:,.0f} confirmed for {biz['name']}. Confirmation number: {confirmation}. New account balance: ${new_balance:,}. Share this with the caller.",
                }))
                print(f"Loan confirmed for {biz['name']}: ${amount:,.0f}, new balance: ${new_balance:,}")

            else:
                await tool_handle.send_error(f"Unknown tool: {tool_name}")

        async def process_output():
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        break

                    if msg.msg_type == "audio":
                        await websocket.send_json({
                            "type": "audio_timing",
                            "start_s": msg.start_s,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                            "interrupted": msg.interrupted,
                        })
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(handle_tool_call(msg.tool_call, msg.tool_call_handle))

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    import traceback
                    traceback.print_exc()
                    break

        async def receive_audio():
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        if data.get("type") == "stop":
                            stop_event.set()
                            await input_handle.close()
                            break
                    elif "bytes" in msg:
                        await input_handle.send_audio(msg["bytes"])
                except WebSocketDisconnect:
                    stop_event.set()
                    await input_handle.close()
                    break
                except Exception as e:
                    print(f"Receive error: {e}")
                    stop_event.set()
                    break

        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for task in state.pending_tasks:
            if not task.done():
                task.cancel()
        state.pending_tasks.clear()
        try:
            await websocket.close()
        except:
            pass


@app.get("/api/businesses")
async def get_businesses():
    """Return all business data including PINs (for frontend display)."""
    return JSONResponse(content=[
        {
            "name": biz["name"],
            "account": biz["account"],
            "balance": biz["balance"],
            "pin": biz["pin"],
            "max_loan": biz["max_loan"],
            "rate": biz["rate"],
        }
        for biz in BUSINESSES.values()
    ])


@app.get("/api/audio-config")
async def audio_config():
    return JSONResponse(content={"pcm": USE_PCM})


# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")


@app.get("/")
async def index():
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse(
        content={"error": "Frontend not found. Place index.html in static/"},
        status_code=404,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
