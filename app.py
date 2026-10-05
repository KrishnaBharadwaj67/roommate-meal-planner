"""
Meal Buddy — A local AI meal-planning assistant for Uma Shreyas.
Built for DEV Hacktoberfest "Build for a Friend" challenge.

Uses Gemma via Ollama (fully offline, open-source AI at the core).
⚠️  This is a helper tool, NOT medical or nutritional advice.
"""

import json
import re
import textwrap
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# ─── CONFIG ──────────────────────────────────────────────────────────────────
# Change this to any Ollama model you've pulled, e.g. "gemma3:4b", "mistral",
# "llama3.2". Just run:  ollama pull <model-name>  then update here.
MODEL_NAME = "gemma3"

OLLAMA_URL = "http://localhost:11434/api/generate"
PROFILE_PATH = Path(__file__).parent / "profile.json"
MAX_RETRIES = 3          # retries for malformed JSON from the model
OLLAMA_TIMEOUT = 120.0   # seconds — small models can be slow on CPU
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Meal Buddy")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def load_profile() -> dict:
    """Load the roommate's profile from the local JSON file."""
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def call_ollama(prompt: str) -> str:
    """
    Send a prompt to the local Ollama instance and return the full response.
    Uses the non-streaming endpoint for simplicity.
    """
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
        },
    }
    resp = httpx.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["response"]


def extract_json(text: str) -> dict | list:
    """
    Extract and parse the first JSON object or array from model output.
    Models sometimes wrap JSON in markdown fences — strip those first.
    """
    # Strip markdown code fences like ```json ... ```
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.strip().rstrip("`")

    # Find the first { or [ and grab everything up to the matching close
    start = None
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            start = i
            break
    if start is None:
        raise ValueError("No JSON found in model output")

    bracket = cleaned[start]
    close = "}" if bracket == "{" else "]"
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == bracket:
            depth += 1
        elif cleaned[i] == close:
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start : i + 1])

    # Fallback: try parsing from start to end
    return json.loads(cleaned[start:])


def build_meal_plan_prompt(profile: dict, pantry: str) -> str:
    """Build the prompt asking Gemma to generate a 3-day meal plan."""
    return textwrap.dedent(f"""\
    You are a helpful Indian home-cook meal planner.

    ## Person
    - Name: {profile['name']}
    - Diet: {profile['diet']}
    - Allergies (HARD constraints — NEVER include these): {', '.join(profile['allergies'])}
    - Dislikes (avoid): {', '.join(profile['dislikes'])}
    - Loves: {', '.join(profile['likes'])}
    - Preferred cuisines: {', '.join(profile['cuisines'])}
    - Weekly budget: {profile['budget_per_week_inr']} INR
    - Kitchen: {profile['kitchen']['burners']} burner(s), {'oven' if profile['kitchen']['has_oven'] else 'no oven'}, max {profile['kitchen']['max_cooking_time_weekday_mins']} min cooking on weekdays

    ## Pantry (ingredients currently available)
    {pantry}

    ## Task
    Create a 3-day meal plan (breakfast, lunch, dinner each day).
    - Prioritise pantry ingredients first.
    - Keep total estimated grocery cost within the weekly budget.
    - Every recipe must be cookable on {profile['kitchen']['burners']} burner(s) in ≤{profile['kitchen']['max_cooking_time_weekday_mins']} min.
    - NEVER include any allergens: {', '.join(profile['allergies'])}.

    ## Output — respond ONLY with this JSON, no other text:
    {{
      "meal_plan": [
        {{
          "day": 1,
          "meals": {{
            "breakfast": {{"name": "...", "ingredients": ["..."], "time_mins": 15, "steps": "..."}},
            "lunch":     {{"name": "...", "ingredients": ["..."], "time_mins": 25, "steps": "..."}},
            "dinner":    {{"name": "...", "ingredients": ["..."], "time_mins": 30, "steps": "..."}}
          }}
        }}
      ],
      "grocery_list": [
        {{"item": "...", "category": "Vegetables", "estimated_cost_inr": 30}}
      ],
      "total_estimated_cost_inr": 0
    }}
    """)


def build_allergy_check_prompt(profile: dict, meal_plan: dict) -> str:
    """Build a prompt asking Gemma to audit the meal plan for allergens."""
    allergens = ", ".join(profile["allergies"])
    plan_json = json.dumps(meal_plan, indent=2)
    return textwrap.dedent(f"""\
    You are a food-allergy safety auditor. Check the following meal plan for
    ANY ingredient that contains or is derived from these allergens:
    {allergens}

    Remember:
    - "peanuts" includes peanut oil, peanut butter, groundnut, groundnut oil.
    - "lactose" includes milk, cheese, cream, butter, ghee, paneer, curd/yogurt, whey.
    - Check EVERY ingredient in EVERY meal.

    Meal plan:
    {plan_json}

    Respond ONLY with this JSON, no other text:
    {{
      "safe": true or false,
      "flagged_items": ["ingredient — reason"]
    }}
    """)


def keyword_allergy_check(profile: dict, meal_plan: dict) -> list[str]:
    """
    Hard-coded keyword scan as a safety net on top of the model check.
    Returns a list of flagged strings (empty = safe).
    """
    # Map each allergy to expanded keywords
    ALLERGY_KEYWORDS: dict[str, list[str]] = {
        "peanuts": [
            "peanut", "groundnut", "peanut oil", "groundnut oil",
            "peanut butter", "monkey nut",
        ],
        "lactose": [
            "milk", "cheese", "cream", "butter", "ghee", "paneer",
            "curd", "yogurt", "yoghurt", "whey", "cream cheese",
            "cottage cheese", "buttermilk", "khoya", "mawa",
            "condensed milk", "malai",
        ],
    }

    flags: list[str] = []
    # Collect every ingredient string from the plan
    all_ingredients: list[str] = []
    for day in meal_plan.get("meal_plan", []):
        for meal_type in ("breakfast", "lunch", "dinner"):
            meal = day.get("meals", {}).get(meal_type, {})
            all_ingredients.extend(meal.get("ingredients", []))

    for allergy in profile.get("allergies", []):
        keywords = ALLERGY_KEYWORDS.get(allergy.lower(), [allergy.lower()])
        for ingredient in all_ingredients:
            ing_lower = ingredient.lower()
            for kw in keywords:
                if kw in ing_lower:
                    flags.append(f"'{ingredient}' may contain {allergy} (matched '{kw}')")
    return flags


def build_swap_prompt(profile: dict, current_plan: dict, day: int, meal_type: str, pantry: str) -> str:
    """Build a prompt to regenerate one specific meal."""
    allergens = ", ".join(profile["allergies"])
    other_meals = []
    for d in current_plan.get("meal_plan", []):
        for mt in ("breakfast", "lunch", "dinner"):
            if d["day"] == day and mt == meal_type:
                continue
            meal = d.get("meals", {}).get(mt, {})
            other_meals.append(f"Day {d['day']} {mt}: {meal.get('name', '?')}")

    return textwrap.dedent(f"""\
    You are a helpful Indian home-cook meal planner.

    The user wants to SWAP Day {day} {meal_type}.
    Keep all other meals the same: {'; '.join(other_meals)}.

    Constraints:
    - Diet: {profile['diet']}
    - NEVER include allergens: {allergens}
    - Dislikes: {', '.join(profile['dislikes'])}
    - Cuisines: {', '.join(profile['cuisines'])}
    - Max cooking time: {profile['kitchen']['max_cooking_time_weekday_mins']} min
    - Burners: {profile['kitchen']['burners']}
    - Pantry: {pantry}

    Respond ONLY with this JSON, no other text:
    {{
      "name": "...",
      "ingredients": ["..."],
      "time_mins": 20,
      "steps": "..."
    }}
    """)


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page with the profile pre-loaded."""
    profile = load_profile()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "profile": profile,
    })


@app.post("/generate")
async def generate_meal_plan(request: Request):
    """
    Accept pantry text, generate a 3-day meal plan via Gemma,
    run allergy checks, retry if unsafe or JSON is bad.
    """
    body = await request.json()
    pantry = body.get("pantry", "")
    profile = load_profile()

    # ── Generate meal plan with retries for bad JSON ──
    meal_plan = None
    for attempt in range(MAX_RETRIES):
        try:
            raw = call_ollama(build_meal_plan_prompt(profile, pantry))
            meal_plan = extract_json(raw)
            # Basic shape validation
            if "meal_plan" not in meal_plan:
                raise ValueError("Missing 'meal_plan' key")
            break
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            if attempt == MAX_RETRIES - 1:
                return {
                    "error": True,
                    "message": f"Model returned invalid JSON after {MAX_RETRIES} attempts. Last error: {e}",
                }

    # ── Allergy safety: keyword check ──
    keyword_flags = keyword_allergy_check(profile, meal_plan)

    # ── Allergy safety: model check ──
    model_flags: list[str] = []
    for attempt in range(MAX_RETRIES):
        try:
            raw_check = call_ollama(build_allergy_check_prompt(profile, meal_plan))
            check_result = extract_json(raw_check)
            if not check_result.get("safe", True):
                model_flags = check_result.get("flagged_items", [])
            break
        except (json.JSONDecodeError, ValueError):
            pass  # If the check itself fails, rely on keyword check

    all_flags = keyword_flags + model_flags
    # Remove duplicate flags
    all_flags = list(dict.fromkeys(all_flags))

    # ── If allergens found, regenerate once ──
    if all_flags:
        for attempt in range(MAX_RETRIES):
            try:
                extra_warning = (
                    f"\n\n⚠️ CRITICAL: The previous plan contained allergens: "
                    f"{'; '.join(all_flags)}. "
                    f"Do NOT include ANY of these: {', '.join(profile['allergies'])}. "
                    f"Double-check every ingredient."
                )
                raw = call_ollama(build_meal_plan_prompt(profile, pantry) + extra_warning)
                meal_plan = extract_json(raw)
                if "meal_plan" not in meal_plan:
                    raise ValueError("Missing 'meal_plan' key")
                break
            except (json.JSONDecodeError, ValueError):
                if attempt == MAX_RETRIES - 1:
                    return {
                        "error": True,
                        "message": "Regeneration after allergy flag also failed.",
                    }

        # Re-check after regeneration
        keyword_flags_2 = keyword_allergy_check(profile, meal_plan)
        all_flags = keyword_flags_2  # update flags
    
    allergy_safe = len(all_flags) == 0

    return {
        "error": False,
        "meal_plan": meal_plan,
        "allergy_check": {
            "passed": allergy_safe,
            "flags": all_flags,
        },
    }


@app.post("/swap")
async def swap_meal(request: Request):
    """Regenerate a single meal in the plan."""
    body = await request.json()
    day = body.get("day")
    meal_type = body.get("meal_type")
    pantry = body.get("pantry", "")
    current_plan = body.get("current_plan", {})
    profile = load_profile()

    new_meal = None
    for attempt in range(MAX_RETRIES):
        try:
            raw = call_ollama(build_swap_prompt(profile, current_plan, day, meal_type, pantry))
            new_meal = extract_json(raw)
            if "name" not in new_meal:
                raise ValueError("Missing 'name' key")
            break
        except (json.JSONDecodeError, ValueError) as e:
            if attempt == MAX_RETRIES - 1:
                return {
                    "error": True,
                    "message": f"Swap failed after {MAX_RETRIES} attempts: {e}",
                }

    # Allergy-check the single swapped meal
    ingredients = new_meal.get("ingredients", [])
    temp_plan = {"meal_plan": [{"day": day, "meals": {meal_type: new_meal}}]}
    keyword_flags = keyword_allergy_check(profile, temp_plan)

    if keyword_flags:
        # Try once more with a stern warning
        for attempt in range(MAX_RETRIES):
            try:
                raw = call_ollama(
                    build_swap_prompt(profile, current_plan, day, meal_type, pantry)
                    + f"\n⚠️ NEVER use: {', '.join(profile['allergies'])}!"
                )
                new_meal = extract_json(raw)
                break
            except (json.JSONDecodeError, ValueError):
                pass
        keyword_flags = keyword_allergy_check(
            profile,
            {"meal_plan": [{"day": day, "meals": {meal_type: new_meal}}]},
        )

    return {
        "error": False,
        "new_meal": new_meal,
        "allergy_check": {
            "passed": len(keyword_flags) == 0,
            "flags": keyword_flags,
        },
    }


# ─── ENTRYPOINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("\n[Meal Buddy] Starting...")
    print(f"    Model  : {MODEL_NAME}")
    print(f"    Ollama : {OLLAMA_URL}")
    print(f"    Profile: {PROFILE_PATH}\n")
    uvicorn.run(app, host="127.0.0.1", port=8000)
