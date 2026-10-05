"""Quick unit tests for Meal Buddy core functions."""
import json
import sys

sys.path.insert(0, ".")
from app import extract_json, keyword_allergy_check, load_profile

# Test 1: extract_json from markdown fences
raw = '```json\n{"meal_plan": [], "grocery_list": [], "total_estimated_cost_inr": 0}\n```'
result = extract_json(raw)
assert "meal_plan" in result, "Failed: extract_json with fences"
print("PASS: extract_json with markdown fences")

# Test 2: extract_json from plain JSON
raw2 = '{"meal_plan": [{"day": 1, "meals": {}}], "grocery_list": []}'
result2 = extract_json(raw2)
assert len(result2["meal_plan"]) == 1
print("PASS: extract_json with plain JSON")

# Test 3: keyword allergy check - should flag milk (lactose) and ghee (lactose)
profile = load_profile()
bad_plan = {
    "meal_plan": [
        {
            "day": 1,
            "meals": {
                "breakfast": {
                    "name": "Cereal",
                    "ingredients": ["milk", "cereal", "sugar"],
                    "time_mins": 5,
                    "steps": "Pour",
                },
                "lunch": {
                    "name": "Rice",
                    "ingredients": ["rice", "dal"],
                    "time_mins": 20,
                    "steps": "Cook",
                },
                "dinner": {
                    "name": "Roti",
                    "ingredients": ["wheat", "ghee"],
                    "time_mins": 25,
                    "steps": "Make",
                },
            },
        }
    ]
}
flags = keyword_allergy_check(profile, bad_plan)
assert len(flags) >= 2, f"Expected >=2 flags, got {len(flags)}: {flags}"
print(f"PASS: keyword allergy check caught {len(flags)} violations: {flags}")

# Test 4: safe plan should pass
safe_plan = {
    "meal_plan": [
        {
            "day": 1,
            "meals": {
                "breakfast": {
                    "name": "Poha",
                    "ingredients": ["poha", "onion", "mustard seeds"],
                    "time_mins": 15,
                    "steps": "Fry",
                },
                "lunch": {
                    "name": "Dal Rice",
                    "ingredients": ["rice", "toor dal", "turmeric"],
                    "time_mins": 25,
                    "steps": "Cook",
                },
                "dinner": {
                    "name": "Veg Pulao",
                    "ingredients": ["rice", "peas", "carrots"],
                    "time_mins": 30,
                    "steps": "Cook",
                },
            },
        }
    ]
}
safe_flags = keyword_allergy_check(profile, safe_plan)
assert len(safe_flags) == 0, f"Expected 0 flags, got {safe_flags}"
print("PASS: safe plan passes allergy check")

# Test 5: extract_json with extra text around it
raw3 = "Here is the plan:\n\n{\"safe\": true, \"flagged_items\": []}\n\nHope that helps!"
result3 = extract_json(raw3)
assert result3["safe"] is True
print("PASS: extract_json strips surrounding text")

print("\nAll 5 tests passed!")
