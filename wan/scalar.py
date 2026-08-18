"""Scalar API reference page.

The CDN URL is pinned to an exact version. Tracking ``@latest`` means a breaking
change published upstream silently breaks the docs page of every already deployed
service, which is exactly the failure mode you cannot debug from your own repo.
"""

import json
from typing import Any, Dict, Optional

#: Bump deliberately, and re-check the page renders when you do.
SCALAR_VERSION = '1.65.1'
SCALAR_CDN = (
    f'https://cdn.jsdelivr.net/npm/@scalar/api-reference@{SCALAR_VERSION}'
    '/dist/browser/standalone.min.js'
)

TEMPLATE = """<!doctype html>
<html>
<head>
  <title>__TITLE__</title>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" href="data:," />
  <style>
    body { margin: 0; }
    #scalar-fallback {
      display: none;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      padding: 2rem;
      line-height: 1.6;
    }
  </style>
</head>
<body>
  <div id="scalar-app"></div>
  <div id="scalar-fallback">
    <h2>API reference failed to load</h2>
    <p>The Scalar bundle could not be fetched from <code>__JS_URL__</code>.</p>
    <p>If this deployment has no internet access, self host the bundle and set
    <code>SCALAR_JS_URL</code> to point at it. The raw spec is always available at
    <a href="__OPENAPI_URL__">__OPENAPI_URL__</a>.</p>
  </div>
  <script src="__JS_URL__" onerror="document.getElementById('scalar-fallback').style.display='block'"></script>
  <script>
    (function () {
      var configuration = __CONFIGURATION__;
      if (window.Scalar && window.Scalar.createApiReference) {
        window.Scalar.createApiReference('#scalar-app', configuration);
      } else {
        document.getElementById('scalar-fallback').style.display = 'block';
      }
    })();
  </script>
</body>
</html>
"""


def render(
    openapi_url: str,
    title: str = 'API Reference',
    theme: str = 'purple',
    dark_mode: bool = True,
    js_url: Optional[str] = None,
    configuration: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the standalone Scalar HTML page for `openapi_url`."""
    js_url = js_url or SCALAR_CDN
    config: Dict[str, Any] = {
        'url': openapi_url,
        'theme': theme,
        'darkMode': dark_mode,
        'hideDownloadButton': False,
        # Keep the deep link in the address bar so docs links are shareable.
        'withDefaultFonts': True,
    }
    if configuration:
        config.update(configuration)

    return (
        TEMPLATE
        .replace('__CONFIGURATION__', json.dumps(config))
        .replace('__OPENAPI_URL__', openapi_url)
        .replace('__JS_URL__', js_url)
        .replace('__TITLE__', title)
    )


#: Backwards compatible with the original module, which exported a raw `html`
#: string containing a `{{openapi_url}}` placeholder.
html = TEMPLATE.replace('__CONFIGURATION__', '{"url": "{{openapi_url}}"}') \
               .replace('__OPENAPI_URL__', '{{openapi_url}}') \
               .replace('__JS_URL__', SCALAR_CDN) \
               .replace('__TITLE__', 'API Reference')
