"""
Insurance (Mutuelle) Demo - Voice AI health insurance agent with PIN authentication

A French voice agent that handles PIN authentication, card replacement,
child enrollment, and reimbursement lookups for a health insurance (mutuelle).

Run with: uvicorn main:app --reload --port 8002
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
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

# ---------------------------------------------------------------------------
# Client data
# ---------------------------------------------------------------------------

CLIENTS = {
    "famille_martin": {
        "name": "Famille Martin",
        "adherent": "MUT-7741-916",
        "pin": "4829",
        "formule": "Confort Plus",
        "enfants": [
            {"prenom": "Lucas", "age": 8},
            {"prenom": "Emma", "age": 5},
        ],
        "carte": "MC-7741-A",
    },
    "couple_dubois": {
        "name": "Couple Dubois",
        "adherent": "MUT-3385-453",
        "pin": "7153",
        "formule": "Essentielle",
        "enfants": [],
        "carte": "MC-3385-A",
    },
    "sophie_leroy": {
        "name": "Sophie Leroy",
        "adherent": "MUT-5562-278",
        "pin": "3061",
        "formule": "Premium",
        "enfants": [
            {"prenom": "Théo", "age": 12},
        ],
        "carte": "MC-5562-A",
    },
}

# ---------------------------------------------------------------------------
# Reimbursement table
# ---------------------------------------------------------------------------

REMBOURSEMENTS = {
    "médecin généraliste": {
        "label": "Médecin généraliste",
        "base_secu": 26.50,
        "secu_pct": 70,
        "confort_plus": 100,
        "essentielle": 80,
        "premium": 100,
        "type": "pct",
    },
    "médecin spécialiste": {
        "label": "Médecin spécialiste",
        "base_secu": 30.00,
        "secu_pct": 70,
        "confort_plus": 90,
        "essentielle": 70,
        "premium": 100,
        "type": "pct",
    },
    "dentiste": {
        "label": "Dentiste (consultation)",
        "base_secu": 23.00,
        "secu_pct": 70,
        "confort_plus": 100,
        "essentielle": 80,
        "premium": 100,
        "type": "pct",
    },
    "extraction dent de sagesse": {
        "label": "Extraction dent de sagesse",
        "base_secu": 83.60,
        "secu_pct": 70,
        "confort_plus": 80,
        "essentielle": 60,
        "premium": 100,
        "type": "pct",
    },
    "ophtalmologue": {
        "label": "Ophtalmologue",
        "base_secu": 30.00,
        "secu_pct": 70,
        "confort_plus": 85,
        "essentielle": 70,
        "premium": 100,
        "type": "pct",
    },
    "kinésithérapeute": {
        "label": "Kinésithérapeute",
        "base_secu": 18.00,
        "secu_pct": 60,
        "confort_plus": 100,
        "essentielle": 80,
        "premium": 100,
        "type": "pct",
    },
    "lunettes": {
        "label": "Lunettes (forfait annuel)",
        "base_secu": None,
        "secu_pct": None,
        "confort_plus": 250,
        "essentielle": 100,
        "premium": 450,
        "type": "forfait",
    },
    "hospitalisation": {
        "label": "Hospitalisation (chambre/jour)",
        "base_secu": None,
        "secu_pct": 80,
        "confort_plus": "100% + 20€/j",
        "essentielle": "100%",
        "premium": "100% + 40€/j",
        "type": "special",
    },
}

# Map formule names to column keys
FORMULE_KEYS = {
    "Confort Plus": "confort_plus",
    "Essentielle": "essentielle",
    "Premium": "premium",
}


def find_client_by_digits(digits: str) -> dict | None:
    digits = digits.strip()
    for client in CLIENTS.values():
        if client["adherent"].endswith(digits):
            return client
    return None


def find_client(name: str) -> dict | None:
    name_lower = name.lower().strip()
    if name_lower in CLIENTS:
        return CLIENTS[name_lower]
    for client in CLIENTS.values():
        if name_lower in client["name"].lower() or client["name"].lower() in name_lower:
            return client
    for client in CLIENTS.values():
        client_words = set(client["name"].lower().split())
        query_words = set(name_lower.split())
        if client_words & query_words:
            return client
    return None


def compute_remboursement(type_soin: str, formule: str) -> dict | None:
    """Compute reimbursement breakdown for a given care type and formule."""
    # Fuzzy match the care type
    type_lower = type_soin.lower().strip()
    soin = None
    for key, val in REMBOURSEMENTS.items():
        if type_lower in key or key in type_lower:
            soin = val
            break
    # Try partial word matching
    if not soin:
        for key, val in REMBOURSEMENTS.items():
            key_words = set(key.split())
            query_words = set(type_lower.split())
            if key_words & query_words:
                soin = val
                break
    if not soin:
        return None

    formule_key = FORMULE_KEYS.get(formule)
    if not formule_key:
        return None

    mutuelle_val = soin[formule_key]

    if soin["type"] == "forfait":
        return {
            "type_soin": soin["label"],
            "formule": formule,
            "mode": "forfait",
            "forfait_annuel": f"{mutuelle_val}€",
            "message": f"Pour {soin['label']}, votre formule {formule} prévoit un forfait annuel de {mutuelle_val}€.",
        }

    if soin["type"] == "special":
        return {
            "type_soin": soin["label"],
            "formule": formule,
            "mode": "special",
            "secu": f"{soin['secu_pct']}%" if soin["secu_pct"] else "—",
            "mutuelle": str(mutuelle_val),
            "message": f"Pour {soin['label']}, la Sécurité sociale couvre {soin['secu_pct']}% et votre formule {formule} couvre {mutuelle_val}.",
        }

    # Standard percentage-based
    base = soin["base_secu"]
    secu_pct = soin["secu_pct"]
    mut_pct = mutuelle_val

    secu_remb = round(base * secu_pct / 100, 2)
    mut_remb = round(base * mut_pct / 100, 2)
    reste = round(max(0, base - mut_remb), 2)

    return {
        "type_soin": soin["label"],
        "formule": formule,
        "mode": "pourcentage",
        "base_secu": f"{base}€",
        "remboursement_secu": f"{secu_remb}€ ({secu_pct}%)",
        "remboursement_mutuelle": f"{mut_remb}€ ({mut_pct}%)",
        "reste_a_charge": f"{reste}€",
        "message": f"Pour {soin['label']} (base Sécu {base}€) : la Sécu rembourse {secu_remb}€ ({secu_pct}%), votre mutuelle {formule} rembourse {mut_remb}€ ({mut_pct}% de la base). Reste à charge : {reste}€.",
    }


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class InsuranceSession:
    authenticated_client: str | None = None
    phase: int = 1  # 1=auth, 2=services
    carte_commandee: bool = False
    enfants_ajoutes: list[str] = field(default_factory=list)
    pending_tasks: list[asyncio.Task] = field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def get_auth_prompt(agent_name: str = "Leo", customer_name: str = "") -> str:
    caller_line = f"\nThe caller's name is {customer_name}. Always address them as {customer_name}." if customer_name else ""
    return f"""You are {agent_name}, a professional and friendly health insurance (mutuelle) phone agent at Mutuelle Santé.

IMPORTANT: You ALWAYS speak in French. Every word you say must be in French.

You help members authenticate and access their account.{caller_line}

YOUR PERSONALITY:
- Professional, calm, and reassuring
- Efficient but never rushed
- You take security seriously

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* — just speak naturally
- Be conversational and natural, like a real phone call
- You ALWAYS speak French

START OF CALL:
You are starting the call. Speak first! Greet the caller immediately and begin the authentication process. Do not wait for the caller to speak.

CURRENT PHASE: AUTHENTICATION

YOUR ONE JOB RIGHT NOW: Authenticate the caller in two steps.

STEP 1 — Member number verification:
- Greet them and explain that their phone number has been matched to an existing account.
- Ask them to confirm the last 3 digits of their member number ("numéro d'adhérent") for security.
- The INSTANT they provide digits, call verifier_compte immediately.
- If verifier_compte returns success=false, tell them the digits don't match and ask to try again.
- If verifier_compte returns success=true, welcome them and move to step 2.

STEP 2 — PIN verification:
- Ask for their 4-digit PIN.
- The INSTANT they provide a PIN, call verifier_pin immediately.
- If verifier_pin returns success=false, tell them the PIN was incorrect and ask to try again.
- If verifier_pin returns success=true, the system will handle the rest.

RULES:
- NEVER ask for digits and PIN at the same time. Always two separate steps.
- Do NOT reveal any account information before full authentication.
- NEVER guess or make up PINs or member numbers. You do not know them.

DIGIT INTERPRETATION — CRITICAL:
The caller is speaking digits aloud in French. Speech recognition often garbles them.
Your job is to interpret whatever they say as the most likely digits and call the tool immediately.
- Do NOT ask the caller to repeat or clarify. Just pick the best interpretation and call the tool.
- Do NOT read back digits to confirm. Just call the tool.
"""


def get_service_prompt(client_name: str, formule: str, agent_name: str = "Leo", customer_name: str = "") -> str:
    caller_line = f"\nThe caller's name is {customer_name}. Always address them as {customer_name}." if customer_name else ""
    return f"""You are {agent_name}, a professional and friendly health insurance (mutuelle) phone agent at Mutuelle Santé.

IMPORTANT: You ALWAYS speak in French. Every word you say must be in French.

YOUR PERSONALITY:
- Professional, calm, and reassuring
- Efficient but never rushed
- You take security seriously

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing* — just speak naturally
- Be conversational and natural, like a real phone call
- You ALWAYS speak French{caller_line}

CURRENT PHASE: SERVICES
Authenticated member: {client_name}
Plan: {formule}

The caller is now authenticated. Ask how you can help them today.

AVAILABLE SERVICES:
1. **Order a new mutuelle card** — If they mention a lost, stolen, or damaged card, or want a new card, call commander_carte immediately.
2. **Add a child to the contract** — If they mention adding a child, ask for the first name and age, then call ajouter_enfant.
3. **Check reimbursement levels** — If they ask about reimbursement for a type of care, call consulter_remboursement with the care type.

RULES:
- Present the three options naturally.
- When the caller chooses, call the appropriate tool IMMEDIATELY.
- After ordering a card, ask "Y a-t-il autre chose que je puisse faire pour vous ?"
- NEVER fabricate tracking numbers or amounts. Only use data from tool results.
- When reporting reimbursement levels, always express them as PERCENTAGES only (e.g. "votre mutuelle rembourse 90%"), never as euro amounts. Do not compute or mention specific euro values.
- NEVER suggest services or actions proactively. Wait for the caller to tell you what they need. Do not list options or make recommendations unless explicitly asked.

WHILE WAITING FOR consulter_remboursement RESULTS:
The lookup takes a few seconds. While waiting, mention the other available services or ask if the caller has other questions. Do NOT ask questions — the results will arrive soon.
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def build_tools() -> list[pygradbot.ToolDef]:
    return [
        pygradbot.ToolDef(
            name="verifier_compte",
            description="Vérifier les 3 derniers chiffres du numéro d'adhérent. Appeler dès que l'appelant fournit ses chiffres.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "digits": {
                        "type": "string",
                        "description": "Les 3 derniers chiffres du numéro d'adhérent"
                    }
                },
                "required": ["digits"]
            }),
        ),
        pygradbot.ToolDef(
            name="verifier_pin",
            description="Vérifier le code PIN à 4 chiffres d'un adhérent après confirmation du numéro. Retourne succès ou échec.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "client_name": {
                        "type": "string",
                        "description": "Le nom de l'adhérent (obtenu via verifier_compte)"
                    },
                    "pin": {
                        "type": "string",
                        "description": "Le code PIN à 4 chiffres fourni par l'appelant"
                    }
                },
                "required": ["client_name", "pin"]
            }),
        ),
        pygradbot.ToolDef(
            name="commander_carte",
            description="Commander une nouvelle carte de mutuelle pour un adhérent authentifié.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "client_name": {
                        "type": "string",
                        "description": "Le nom de l'adhérent authentifié"
                    }
                },
                "required": ["client_name"]
            }),
        ),
        pygradbot.ToolDef(
            name="ajouter_enfant",
            description="Ajouter un enfant sur le contrat d'un adhérent authentifié.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "client_name": {
                        "type": "string",
                        "description": "Le nom de l'adhérent authentifié"
                    },
                    "prenom": {
                        "type": "string",
                        "description": "Le prénom de l'enfant à ajouter"
                    },
                    "age": {
                        "type": "integer",
                        "description": "L'âge de l'enfant"
                    }
                },
                "required": ["client_name", "prenom", "age"]
            }),
        ),
        pygradbot.ToolDef(
            name="consulter_remboursement",
            description="Consulter les niveaux de remboursement pour un type de soin selon la formule de l'adhérent. La recherche prend quelques secondes — continuez à discuter avec l'appelant en attendant !",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "type_soin": {
                        "type": "string",
                        "description": "Le type de soin (ex: médecin généraliste, spécialiste, dentiste, ophtalmologue, kinésithérapeute, lunettes, hospitalisation)"
                    }
                },
                "required": ["type_soin"]
            }),
        ),
    ]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting Insurance (Mutuelle) Demo...")
    yield
    print("Shutting down...")


app = FastAPI(title="Insurance Demo", lifespan=lifespan)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    state = InsuranceSession()

    try:
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        customer_last_name = start_msg.get("customer", "Martin")
        CUSTOMER_TITLES = {
            "Martin": "Madame Martin",
            "Dubois": "Monsieur Dubois",
            "Leroy": "Madame Leroy",
        }
        customer_name = CUSTOMER_TITLES.get(customer_last_name, customer_last_name)
        agent_name = start_msg.get("agent", "Leo")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        print(f"Starting insurance chat (agent: {agent_name}, customer: {customer_name}, padding_bonus: {padding_bonus})")

        AGENT_VOICES = {
            "Leo": pygradbot.flagship_voice("Leo").voice_id,
            "Constance": "Y4iYxS8PBX",
        }
        voice_id = AGENT_VOICES.get(agent_name, AGENT_VOICES["Leo"])
        tools = build_tools()

        config = pygradbot.SessionConfig(
            voice_id=voice_id,
            instructions=get_auth_prompt(agent_name),
            language=pygradbot.Lang.Fr,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules="fr",
                assistant_speaks_first=True,
            ),
        )

        input_handle, output_handle = await pygradbot.run(
            **_CLIENT_CONFIG,
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.Pcm if USE_PCM else pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        def make_config(instructions: str) -> pygradbot.SessionConfig:
            return pygradbot.SessionConfig(
                voice_id=voice_id,
                instructions=instructions,
                language=pygradbot.Lang.Fr,
                tools=tools,
                **merge_overrides(_OVERRIDES,
                    flush_duration_s=FLUSH_FOR_S,
                    padding_bonus=padding_bonus,
                    rewrite_rules="fr",
                ),
            )

        async def handle_tool_call(tool_call, tool_handle):
            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)
            print(f"Tool call: {tool_name} - {args}")

            if tool_name == "verifier_compte":
                digits = args.get("digits", "")
                client = find_client_by_digits(digits)

                if not client:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Aucun adhérent trouvé avec les chiffres '{digits}'. Demande à l'appelant de réessayer.",
                    }))
                    return

                await tool_handle.send(json.dumps({
                    "success": True,
                    "client_name": client["name"],
                    "message": f"Numéro d'adhérent confirmé. C'est le compte de {client['name']}. The caller should be addressed as {customer_name}. Welcome them and ask for their 4-digit PIN.",
                }))
                print(f"Account confirmed: {client['name']} (digits: {digits})")

            elif tool_name == "verifier_pin":
                client_name = args.get("client_name", "")
                pin = args.get("pin", "")
                client = find_client(client_name)

                if not client:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Adhérent '{client_name}' non trouvé.",
                    }))
                    return

                if client["pin"] == pin.strip():
                    state.authenticated_client = client["name"]
                    state.phase = 2

                    await websocket.send_json({
                        "type": "auth_success",
                        "client": client["name"],
                    })

                    phase2 = get_service_prompt(client["name"], client["formule"], agent_name, customer_name)
                    await input_handle.send_config(make_config(phase2))
                    print(f"Authenticated: {client['name']}, switched to phase 2")

                    await tool_handle.send(json.dumps({
                        "success": True,
                        "message": f"PIN verified. The caller is authenticated as {customer_name}, plan {client['formule']}. Welcome them and ask how you can help.",
                    }))
                else:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": "Code PIN incorrect. Demande à l'appelant de réessayer.",
                    }))

            elif tool_name == "commander_carte":
                client_name = args.get("client_name", "")
                client = find_client(client_name)

                if not client:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Adhérent '{client_name}' non trouvé.",
                    }))
                    return

                tracking = "CM-" + "".join(random.choice("123456789") for _ in range(4))
                state.carte_commandee = True

                await websocket.send_json({
                    "type": "carte_commandee",
                    "client": client["name"],
                    "tracking": tracking,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "tracking": tracking,
                    "message": f"Nouvelle carte de mutuelle commandée pour {client['name']}. Numéro de suivi : {tracking}. La carte arrivera sous 5 à 7 jours ouvrés. Partage cette information et demande s'il y a autre chose.",
                }))
                print(f"Card ordered for {client['name']}: {tracking}")

            elif tool_name == "ajouter_enfant":
                client_name = args.get("client_name", "")
                prenom = args.get("prenom", "")
                age = args.get("age", 0)
                client = find_client(client_name)

                if not client:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": f"Adhérent '{client_name}' non trouvé.",
                    }))
                    return

                client["enfants"].append({"prenom": prenom, "age": age})
                state.enfants_ajoutes.append(prenom)

                await websocket.send_json({
                    "type": "enfant_ajoute",
                    "client": client["name"],
                    "prenom": prenom,
                    "age": age,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "message": f"{prenom} ({age} ans) a été ajouté(e) sur le contrat de {client['name']}. Confirme l'ajout à l'appelant et demande s'il y a autre chose.",
                }))
                print(f"Child added for {client['name']}: {prenom}, {age} ans")

            elif tool_name == "consulter_remboursement":
                type_soin = args.get("type_soin", "")
                client = find_client(state.authenticated_client) if state.authenticated_client else None

                if not client:
                    await tool_handle.send(json.dumps({
                        "success": False,
                        "message": "Aucun adhérent authentifié.",
                    }))
                    return

                async def deferred_remboursement(type_soin, client, tool_handle):
                    print(f"Looking up reimbursement for {type_soin} / {client['formule']} (2s delay)")
                    await asyncio.sleep(2)

                    result = compute_remboursement(type_soin, client["formule"])
                    if not result:
                        await tool_handle.send(json.dumps({
                            "success": False,
                            "message": f"Type de soin '{type_soin}' non trouvé dans notre grille. Les types disponibles sont : médecin généraliste, médecin spécialiste, dentiste, extraction dent de sagesse, ophtalmologue, kinésithérapeute, lunettes, hospitalisation.",
                        }))
                        return

                    await tool_handle.send(json.dumps({
                        "success": True,
                        **result,
                    }))

                task = asyncio.create_task(deferred_remboursement(type_soin, client, tool_handle))
                state.pending_tasks.append(task)
                task.add_done_callback(lambda t: state.pending_tasks.remove(t) if t in state.pending_tasks else None)

            else:
                await tool_handle.send_error(f"Outil inconnu : {tool_name}")

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
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": str(e) if DEBUG else "Une erreur est survenue",
                        })
                    except:
                        pass
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
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e) if DEBUG else "Une erreur est survenue au démarrage",
            })
        except:
            pass
    finally:
        for task in state.pending_tasks:
            if not task.done():
                task.cancel()
        state.pending_tasks.clear()
        try:
            await websocket.close()
        except:
            pass


@app.get("/api/clients")
async def get_clients():
    """Return all client data including PINs (for frontend display)."""
    return JSONResponse(content=[
        {
            "name": client["name"],
            "adherent": client["adherent"],
            "pin": client["pin"],
            "formule": client["formule"],
            "enfants": client["enfants"],
            "carte": client["carte"],
        }
        for client in CLIENTS.values()
    ])


@app.get("/api/remboursements")
async def get_remboursements():
    """Return the reimbursement table for frontend display."""
    return JSONResponse(content=[
        {
            "label": soin["label"],
            "base_secu": f"{soin['base_secu']}€" if soin["base_secu"] else "—",
            "secu_pct": f"{soin['secu_pct']}%" if soin["secu_pct"] else "—",
            "confort_plus": str(soin["confort_plus"]) + ("€" if soin["type"] == "forfait" else "%" if soin["type"] == "pct" else ""),
            "essentielle": str(soin["essentielle"]) + ("€" if soin["type"] == "forfait" else "%" if soin["type"] == "pct" else ""),
            "premium": str(soin["premium"]) + ("€" if soin["type"] == "forfait" else "%" if soin["type"] == "pct" else ""),
        }
        for soin in REMBOURSEMENTS.values()
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
    uvicorn.run(app, host="0.0.0.0", port=8002)
