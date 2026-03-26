"""
Chick-fil-A Vendor Demo - Voice AI ordering agent

A multilingual voice agent that helps customers browse the menu,
customize items, and place orders.

Run with: uvicorn main:app --reload
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket

import gradbot
from gradbot.fastapi import websocket_chat_handler, setup_demo_routes
from gradbot.demo_config import load_config, session_config_overrides, merge_overrides, client_config

gradbot.init_logging()
logger = logging.getLogger(__name__)

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

# Language → (voice_id, Lang enum, rewrite_rules code)
# English uses a custom voice; other languages use flagship voices.
LANG_CONFIG = {
    "en": ("3jUdJyOi9pgbxBTK", gradbot.Lang.En, "en"),
    "fr": (gradbot.flagship_voice("Elise").voice_id, gradbot.Lang.Fr, "fr"),
    "es": (gradbot.flagship_voice("Valentina").voice_id, gradbot.Lang.Es, "es"),
    "de": (gradbot.flagship_voice("Mia").voice_id, gradbot.Lang.De, "de"),
    "pt": (gradbot.flagship_voice("Alice").voice_id, gradbot.Lang.Pt, "pt"),
}

# Load menu data
MENU_PATH = Path(__file__).parent / "menu.json"
with open(MENU_PATH) as f:
    MENU_DATA = json.load(f)

TRANSLATIONS_PATH = Path(__file__).parent / "menu_translations.json"
with open(TRANSLATIONS_PATH) as f:
    MENU_TRANSLATIONS = json.load(f)


def translate_menu_items(items: list[dict], lang: str) -> list[dict]:
    """Return a translated copy of menu items for the given language."""
    if lang == "en":
        return items
    item_tr = MENU_TRANSLATIONS.get("items", {})
    opt_tr = MENU_TRANSLATIONS.get("options", {})
    translated = []
    for item in items:
        t = item_tr.get(item["id"], {}).get(lang, {})
        new_item = {**item}
        if t.get("name"):
            new_item["name"] = t["name"]
        if t.get("description"):
            new_item["description"] = t["description"]
        # Translate options
        if item.get("options"):
            new_opts = {}
            for opt_key, opt_vals in item["options"].items():
                tr_map = opt_tr.get(opt_key, {}).get(lang, {})
                new_opts[opt_key] = [tr_map.get(v, v) for v in opt_vals]
            new_item["options"] = new_opts
        translated.append(new_item)
    return translated


def translate_category_name(cat_key: str, cat_name: str, lang: str) -> str:
    """Return the translated category name."""
    if lang == "en":
        return cat_name
    return MENU_TRANSLATIONS.get("categories", {}).get(cat_key, {}).get(lang, cat_name)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class OrderItem:
    """A single item in the order."""
    item_id: str
    item_name: str
    category: str
    price: float
    customizations: dict[str, str | list[str]] = field(default_factory=dict)


@dataclass
class OrderState:
    """Tracks the current order."""
    items: list[OrderItem] = field(default_factory=list)
    order_placed: bool = False
    lang: str = "en"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_menu_item(item_id: str) -> tuple[dict | None, str | None]:
    """Find a menu item by ID and return (item, category)."""
    for cat_key, cat_data in MENU_DATA["categories"].items():
        for item in cat_data["items"]:
            if item["id"] == item_id:
                return item, cat_key
    return None, None


def translate_item_name(item_id: str, fallback: str, lang: str) -> str:
    """Translate a single item name by its ID."""
    if lang == "en":
        return fallback
    return MENU_TRANSLATIONS.get("items", {}).get(item_id, {}).get(lang, {}).get("name", fallback)


def order_items_json(state: OrderState) -> list[dict]:
    """Serialize current order items for the frontend."""
    return [
        {
            "name": translate_item_name(item.item_id, item.item_name, state.lang),
            "price": item.price,
            "customizations": item.customizations,
        }
        for item in state.items
    ]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def get_system_prompt(state: OrderState) -> str:
    """System prompt for the Chick-fil-A ordering agent."""

    # Build concise menu overview with item names AND IDs for tool calling
    menu_overview = "MENU ITEMS (with IDs for add_to_order):\n"
    for cat_key, cat_data in MENU_DATA["categories"].items():
        menu_overview += f"\n{cat_data['name']}:\n"
        for item in cat_data["items"]:
            menu_overview += f"  - {item['name']} (id: {item['id']})\n"

    # Current order summary
    order_summary = ""
    total_price = 0.0
    if state.items:
        order_summary = "\n\nCURRENT ORDER:\n"
        for idx, item in enumerate(state.items, 1):
            order_summary += f"{idx}. {item.item_name} - ${item.price:.2f}"
            if item.customizations:
                custom_str = ", ".join(f"{k}: {v}" for k, v in item.customizations.items())
                order_summary += f" ({custom_str})"
            order_summary += "\n"
            total_price += item.price
        order_summary += f"\nCurrent Total: ${total_price:.2f}"
    else:
        order_summary = "\n\nCURRENT ORDER: Empty"

    return f"""You are a friendly and efficient restaurant ordering assistant.

You help customers browse the menu, customize their items, and place orders.

MULTILINGUAL SUPPORT:
- You speak English, French, Spanish, German, and Portuguese
- ALWAYS reply in the same language the customer is speaking
- If the customer switches language mid-conversation, call switch_language IMMEDIATELY with the new language code, THEN reply in that language
- Language codes: "en" (English), "fr" (French), "es" (Spanish), "de" (German), "pt" (Portuguese)
- Current language: {state.lang}
- When switching languages, maintain the same casual, friendly tone — just in the new language
- Use natural idioms and expressions for that language, not stiff translations

YOUR PERSONALITY:
- Friendly and laid-back, like a real fast food cashier
- Casual and conversational, not overly scripted or formal
- Helpful but not pushy - you're here to take orders, not oversell

SPEAKING STYLE (BE REALISTIC):
- Talk like a real person at a fast food counter - casual, natural, sometimes imperfect
- Use natural filler words and slight hesitations appropriate for the current language
- Keep it conversational and vary your responses - don't sound robotic or repetitive
- Keep responses SHORT - 1-2 sentences max, like a real cashier would
- When saying prices, drop .00 decimals (say "$8" not "$8.00", but "$8.45" is fine)
- NEVER say "My pleasure" - just use casual acknowledgments
- NEVER use action annotations like *smiles* or *typing* - just speak naturally

CONVERSATION AWARENESS:
- Pay close attention to what you JUST said in your last response
- When the customer says "that" or "it" or asks a vague question, they're referring to what you JUST mentioned
- If you just suggested fries and they say "too much", they mean the fries, not other items in the order
- Follow the conversation flow - their response is usually about YOUR last suggestion
- If truly ambiguous, ask a clarifying question: "Do you mean too much food total, or...?"

{menu_overview}

{order_summary}

CRITICAL TOOL CALLING RULES:
- NEVER announce tool calls. Do NOT say "let me check the menu" or "I'm adding that to your order"
- Just call the tool silently, then respond naturally based on the result
- Call the tool FIRST, THEN speak
- If you need to call a tool, do it without mentioning it - the customer will see the results on screen

MANDATORY TOOL CALL TRIGGERS (NEVER SKIP THESE):
- Menu questions ("what's on the menu", "what do you have", "tell me about X") → MUST call show_menu first
- Ordering NEW items ("I'll have X", "I want X", "get me X", "add X") → MUST call add_to_order first
- Modifying existing items ("change the cheese", "make it no pickles", "switch to multigrain") → MUST call modify_item first
- Removing ("remove item", "take off item 1", "cancel the X") → MUST call remove_from_order first
- Viewing order ("what's my order", "what do I have", "my total") → MUST call view_order first
- Checkout ("I'm ready", "checkout", "that's all", "place my order") → MUST call place_order first
- Language change (customer speaks a different language than current) → MUST call switch_language FIRST, then reply in the new language

TOOL CALL ENFORCEMENT:
- Do NOT just talk about it - CALL THE TOOL FIRST, THEN talk
- Talking about menu items without calling show_menu is FORBIDDEN
- Talking about adding items without calling add_to_order is FORBIDDEN
- Every tool call trigger REQUIRES the tool call - NO EXCEPTIONS
- You CAN and SHOULD make multiple tool calls in one turn if needed
- If you're not sure which item they want, call show_menu first to see options

MENU PRESENTATION (CRITICAL FOR VOICE):
- NEVER read the full menu out loud - that's unnatural and tedious
- When asked about the menu, mention high-level categories: "We have entrees like sandwiches and nuggets, sides, drinks, and desserts. What sounds good to you?"
- Only give 2-3 specific examples when mentioning a category
- If they ask about a specific category, mention 2-3 popular items from that category
- The full menu is displayed on screen - you don't need to read it all
- Let them ask for details about specific items

IMPORTANT RULES:
- ALWAYS call show_menu when customers ask about the menu - you'll get full item details in the tool result
- NEVER make up menu items or prices - only use information from tool call results
- Ask about customization options when adding items that have them (bread type, sauces, extras, etc.)
- Suggest complementary items naturally ("Would you like fries with that?")
- If they ask about an item not on the menu, politely explain we don't have it and suggest alternatives
- Confirm the order total before placing the order
- Be proactive about asking "Anything else?" after adding items

TOOLS AVAILABLE:
- show_menu: Display the full menu or a specific category (entrees, sides, drinks, desserts)
- add_to_order: Add an item to the order - USE THE ITEM ID from the menu list above (e.g., "spicy_sandwich" not "Spicy Chicken Sandwich")
- modify_item: Change an existing order item - USE THIS when they want to change options/customizations on an already-ordered item
- view_order: Show the current order and total price
- remove_from_order: Remove an item from the order by its position number (1 for first item, 2 for second, etc.)
- place_order: Finalize the order (ask for the customer's name first)
- switch_language: Switch voice and language when the customer speaks a different language (en/fr/es/de/pt)

CRITICAL:
- When calling add_to_order, you MUST use the item ID (like "spicy_sandwich"), not the display name!
- When they want to CHANGE an existing item's options, use modify_item (NOT remove + add)

Start by greeting the customer warmly in {dict(en="English", fr="French", es="Spanish", de="German", pt="Portuguese").get(state.lang, "English")} and asking how you can help them today!
"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def build_tools() -> list[gradbot.ToolDef]:
    """Build the tool definitions for the ordering agent."""

    return [
        gradbot.ToolDef(
            name="show_menu",
            description="Display the menu to the customer. Can show full menu or a specific category.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["all", "entrees", "sides", "drinks", "desserts"],
                        "description": "Which category to show. Use 'all' for the full menu."
                    }
                },
                "required": ["category"]
            }),
        ),
        gradbot.ToolDef(
            name="add_to_order",
            description="Add an item to the customer's order with customizations.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "The ID of the menu item (e.g., 'original_sandwich', 'waffle_fries_medium')"
                    },
                    "customizations": {
                        "type": "object",
                        "description": "Customization options as key-value pairs (e.g., {'bread': 'Multigrain Bun', 'extras': ['Add Cheese', 'Extra Pickles']})",
                        "additionalProperties": True
                    }
                },
                "required": ["item_id"]
            }),
        ),
        gradbot.ToolDef(
            name="modify_item",
            description="Modify an existing item in the order by replacing it with a new version with different customizations.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "position": {
                        "type": "integer",
                        "description": "The position number of the item to modify (1 for first item, 2 for second, etc.)"
                    },
                    "item_id": {
                        "type": "string",
                        "description": "The ID of the menu item (usually same as original, e.g., 'spicy_sandwich')"
                    },
                    "customizations": {
                        "type": "object",
                        "description": "The NEW customization options to replace the old ones",
                        "additionalProperties": True
                    }
                },
                "required": ["position", "item_id", "customizations"]
            }),
        ),
        gradbot.ToolDef(
            name="view_order",
            description="Show the customer their current order with all items and the total price.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        gradbot.ToolDef(
            name="remove_from_order",
            description="Remove an item from the order by its position number (1-indexed).",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "position": {
                        "type": "integer",
                        "description": "The position number of the item to remove (1 for first item, 2 for second, etc.)"
                    }
                },
                "required": ["position"]
            }),
        ),
        gradbot.ToolDef(
            name="place_order",
            description="Finalize and place the customer's order. Get their name first!",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "customer_name": {
                        "type": "string",
                        "description": "The customer's name for the order"
                    }
                },
                "required": ["customer_name"]
            }),
        ),
        gradbot.ToolDef(
            name="switch_language",
            description="Switch the conversation language when the customer speaks a different language. Call this BEFORE replying.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "fr", "es", "de", "pt"],
                        "description": "Language code to switch to"
                    }
                },
                "required": ["language"]
            }),
        ),
    ]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Chick-fil-A Vendor Demo")


@app.websocket("/ws/order")
async def websocket_order(websocket: WebSocket):
    state = OrderState()
    tools = build_tools()

    def make_config() -> gradbot.SessionConfig:
        """Build a SessionConfig from current state."""
        vid, l_enum, rw = LANG_CONFIG[state.lang]
        return gradbot.SessionConfig(
            voice_id=vid,
            instructions=get_system_prompt(state),
            language=l_enum,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=0.0,
                rewrite_rules=rw,
                assistant_speaks_first=True,
            ),
        )

    def on_start(msg: dict) -> gradbot.SessionConfig:
        lang = msg.get("language", "en")
        if lang in LANG_CONFIG:
            state.lang = lang
        logger.info("Starting Chick-fil-A ordering session (lang=%s)", state.lang)
        return make_config()

    async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        logger.info("Tool call: %s - %s", tool_name, args)

        if tool_name == "show_menu":
            category = args.get("category", "all")
            lang = state.lang

            if category == "all":
                menu_items = []
                for cat_key, cat_data in MENU_DATA["categories"].items():
                    menu_items.append({
                        "category": translate_category_name(cat_key, cat_data["name"], lang),
                        "items": translate_menu_items(cat_data["items"], lang),
                    })

                await websocket.send_json({
                    "type": "menu_display",
                    "category": "Full Menu",
                    "menu": menu_items,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "message": "Full menu is now displayed. Present the categories and ask what they're interested in!",
                }))
            else:
                cat_data = MENU_DATA["categories"].get(category)
                if cat_data:
                    cat_name = translate_category_name(category, cat_data["name"], lang)
                    items = translate_menu_items(cat_data["items"], lang)

                    await websocket.send_json({
                        "type": "menu_display",
                        "category": cat_name,
                        "menu": [{"category": cat_name, "items": items}],
                    })

                    await tool_handle.send(json.dumps({
                        "success": True,
                        "category": cat_name,
                        "items": items,
                        "message": f"{cat_name} menu is displayed. Describe the options and help them choose!",
                    }))
                else:
                    await tool_handle.send_error(f"Category '{category}' not found")

        elif tool_name == "add_to_order":
            item_id = args.get("item_id")
            customizations = args.get("customizations", {})

            menu_item, category = find_menu_item(item_id)
            if not menu_item:
                await tool_handle.send_error(f"Item '{item_id}' not found in menu")
                return

            state.items.append(OrderItem(
                item_id=item_id,
                item_name=menu_item["name"],
                category=category,
                price=menu_item["price"],
                customizations=customizations,
            ))

            await input_handle.send_config(make_config())

            await websocket.send_json({
                "type": "order_updated",
                "items": order_items_json(state),
                "total": sum(item.price for item in state.items),
            })

            custom_str = ""
            if customizations:
                custom_str = " with " + ", ".join(f"{k}: {v}" for k, v in customizations.items())

            await tool_handle.send(json.dumps({
                "success": True,
                "item_added": menu_item["name"],
                "price": menu_item["price"],
                "customizations": customizations,
                "message": f"Added {menu_item['name']}{custom_str} to the order. Ask if they'd like anything else!",
            }))

        elif tool_name == "modify_item":
            position = args.get("position")
            item_id = args.get("item_id")
            customizations = args.get("customizations", {})

            if position < 1 or position > len(state.items):
                await tool_handle.send_error(f"Invalid position {position}. Order has {len(state.items)} items.")
                return

            menu_item, category = find_menu_item(item_id)
            if not menu_item:
                await tool_handle.send_error(f"Item '{item_id}' not found in menu")
                return

            old_item = state.items.pop(position - 1)
            state.items.insert(position - 1, OrderItem(
                item_id=item_id,
                item_name=menu_item["name"],
                category=category,
                price=menu_item["price"],
                customizations=customizations,
            ))

            await input_handle.send_config(make_config())

            await websocket.send_json({
                "type": "order_updated",
                "items": order_items_json(state),
                "total": sum(item.price for item in state.items),
            })

            custom_str = ""
            if customizations:
                custom_str = " with " + ", ".join(f"{k}: {v}" for k, v in customizations.items())

            await tool_handle.send(json.dumps({
                "success": True,
                "modified_item": menu_item["name"],
                "old_item": old_item.item_name,
                "message": f"Updated item {position} to {menu_item['name']}{custom_str}. Confirm the change!",
            }))

        elif tool_name == "view_order":
            if not state.items:
                await tool_handle.send(json.dumps({
                    "success": True,
                    "items": [],
                    "total": 0.0,
                    "message": "The order is currently empty. Ask what they'd like to order!",
                }))
            else:
                total = sum(item.price for item in state.items)
                items_list = [
                    {"position": idx, "name": item.item_name, "price": item.price, "customizations": item.customizations}
                    for idx, item in enumerate(state.items, 1)
                ]

                await websocket.send_json({
                    "type": "order_updated",
                    "items": order_items_json(state),
                    "total": total,
                })

                await tool_handle.send(json.dumps({
                    "success": True,
                    "items": items_list,
                    "total": total,
                    "message": f"Read out the order items and total (${total:.2f}). Ask if they want to add anything else or if they're ready to checkout!",
                }))

        elif tool_name == "remove_from_order":
            position = args.get("position")

            if position < 1 or position > len(state.items):
                await tool_handle.send_error(f"Invalid position {position}. Order has {len(state.items)} items.")
                return

            removed_item = state.items.pop(position - 1)

            await input_handle.send_config(make_config())

            await websocket.send_json({
                "type": "order_updated",
                "items": order_items_json(state),
                "total": sum(item.price for item in state.items),
            })

            await tool_handle.send(json.dumps({
                "success": True,
                "removed_item": removed_item.item_name,
                "message": f"Removed {removed_item.item_name} from the order. Confirm the removal and ask what else they'd like!",
            }))

        elif tool_name == "place_order":
            customer_name = args.get("customer_name", "Guest")

            if not state.items:
                await tool_handle.send_error("Cannot place an empty order. Add items first!")
                return

            state.order_placed = True
            total = sum(item.price for item in state.items)

            await websocket.send_json({
                "type": "order_placed",
                "customer_name": customer_name,
                "items": order_items_json(state),
                "total": total,
            })

            await tool_handle.send(json.dumps({
                "success": True,
                "customer_name": customer_name,
                "total": total,
                "message": f"Order placed for {customer_name}! Total is ${total:.2f}. Thank them warmly and say 'My pleasure!' Tell them their order will be ready soon!",
            }))

        elif tool_name == "switch_language":
            new_lang = args.get("language", "en")
            if new_lang not in LANG_CONFIG:
                await tool_handle.send_error(f"Unsupported language: {new_lang}")
                return

            state.lang = new_lang
            await input_handle.send_config(make_config())
            logger.info("Switched language to: %s", new_lang)

            await tool_handle.send(json.dumps({
                "success": True,
                "language": new_lang,
                "message": f"Language switched to {new_lang}. Continue the conversation in this language.",
            }))

        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")

    await websocket_chat_handler(
        websocket,
        on_start=on_start,
        on_tool_call=handle_tool_call,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


setup_demo_routes(app, static_dir=Path(__file__).parent / "static", use_pcm=USE_PCM)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
