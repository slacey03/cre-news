"""Embed articles.json into dashboard.html as inline data so it works on double-click."""
import json

with open('articles.json') as f:
    data = json.load(f)

with open('dashboard.html') as f:
    html = f.read()

# Build the inline data block
inline_block = f"""<script id="embedded-data" type="application/json">
{json.dumps(data, indent=2)}
</script>"""

# Replace the loadData function to read from the embedded block
old_loader = """async function loadData() {
  try {
    const res = await fetch('articles.json');
    DATA = await res.json();
    renderStats();
    render();
  } catch (err) {
    document.getElementById('articles').innerHTML =
      '<div class="empty">Could not load articles.json. Run <code>python scraper.py</code> first.</div>';
  }
}"""

new_loader = """function loadData() {
  try {
    const el = document.getElementById('embedded-data');
    if (el) {
      DATA = JSON.parse(el.textContent);
      renderStats();
      render();
      return;
    }
    // Fallback: try to fetch articles.json (works if served via http)
    fetch('articles.json').then(r => r.json()).then(d => {
      DATA = d;
      renderStats();
      render();
    }).catch(() => {
      document.getElementById('articles').innerHTML =
        '<div class="empty">No data available. Re-run the embed script after refreshing articles.json.</div>';
    });
  } catch (err) {
    document.getElementById('articles').innerHTML =
      '<div class="empty">Error loading data: ' + err.message + '</div>';
  }
}"""

html = html.replace(old_loader, new_loader)

# Insert the inline data block right before the main script
html = html.replace('<script>\nlet DATA = null;', inline_block + '\n<script>\nlet DATA = null;')

with open('dashboard.html', 'w') as f:
    f.write(html)

print(f"Embedded {len(data['articles'])} articles into dashboard.html")
print(f"File size: {len(html):,} bytes")
