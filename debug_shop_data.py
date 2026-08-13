"""Debug script to examine shop page __NUXT_DATA__ structure"""
import httpx
import re
import json
from pathlib import Path

shop_url = "https://shop.line.me/@mergeth"

print(f"Fetching {shop_url}...")
resp = httpx.get(shop_url, timeout=15)
html = resp.text

print(f"Status: {resp.status_code}")
print(f"HTML length: {len(html)} chars")

# Extract __NUXT_DATA__
m = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html)
if not m:
    print("❌ __NUXT_DATA__ not found")
    exit(1)

print("✅ Found __NUXT_DATA__")
data = json.loads(m.group(1).strip())

print(f"\nData type: {type(data)}")
if isinstance(data, list):
    print(f"Data length: {len(data)} items")
    print(f"\nFirst 10 items:")
    for i, item in enumerate(data[:10]):
        print(f"  [{i}] {type(item).__name__}: {str(item)[:100]}")
elif isinstance(data, dict):
    print(f"Data keys: {list(data.keys())[:20]}")

# Save full data to file
output_file = "debug_shop_nuxt_data.json"
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"\n✅ Full data saved to {output_file}")

# Try to find products
def find_products(obj, depth=0, path=""):
    """Recursively search for product-like objects"""
    if depth > 20:
        return []
    
    products = []
    
    if isinstance(obj, dict):
        # Check if this looks like a product
        has_id = "id" in obj or "productId" in obj
        has_name = "name" in obj or "productName" in obj or "title" in obj
        
        if has_id:
            pid = obj.get("id") or obj.get("productId")
            if isinstance(pid, int) and pid > 10000:
                name = obj.get("name") or obj.get("productName") or obj.get("title") or ""
                products.append({
                    "path": path,
                    "id": pid,
                    "name": str(name)[:50],
                    "keys": list(obj.keys())[:10]
                })
        
        # Recurse into dict values
        for key, val in obj.items():
            products.extend(find_products(val, depth + 1, f"{path}.{key}"))
    
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            products.extend(find_products(item, depth + 1, f"{path}[{i}]"))
    
    return products

print("\n🔍 Searching for product objects...")
found = find_products(data)

if found:
    print(f"✅ Found {len(found)} product-like objects:\n")
    for p in found[:10]:
        print(f"  ID: {p['id']}")
        print(f"  Name: {p['name']}")
        print(f"  Path: {p['path']}")
        print(f"  Keys: {p['keys']}")
        print()
else:
    print("❌ No products found with current logic")
