Hamingjur project guide and API reference
=======================================

Open index.html directly in a browser. It is a standalone page: CSS, JavaScript,
examples, and the complete source-derived API reference are embedded. Search,
module filters, API permalinks, theme switching, and example previews work offline.
Copy buttons use the clipboard when available and otherwise select the code.

From the Hamingjur repository root, regenerate with Python 3.9+:

    python site/hamingjur-docs/build.py --export docs/hamingjur.html

The builder reads Python syntax without importing the library. It preserves all
public declarations in the supported source modules, docstrings, constructor and
method signatures, class fields and enum values. Private modules and private
methods are omitted; inherited APIs remain under their defining class. Native
HTML disclosure controls leave the reference readable without JavaScript.

The build writes index.html and dist/index.html in this directory and optionally
exports another standalone copy. If using this Site's source repository apart
from the library checkout, pass --package-root /path/to/hamingjur.

template.html is the editable page template. API_REFERENCE, VERSION, ENTRY_COUNT,
MODULE_COUNT and MODULE_OPTIONS are replaced by the builder. Changes to Python
APIs require regeneration; this is a snapshot of the checkout, not a live API.

Production hosting
------------------

Homepage: https://hamingjur.goblinreactor.com
R2 bucket: site-hamingjur-goblinreactor-com
Worker: hamingjur-docs

The generated dist/index.html lives in R2. worker.mjs serves it at the domain
root, with HTML content type, ETags, HEAD support, and streaming response bodies.
wrangler.jsonc records the account, domain and bucket binding.

Rebuild, test the Worker, and deploy using a Cloudflare token file:

    python site/hamingjur-docs/build.py --export docs/hamingjur.html
    node --test site/hamingjur-docs/worker.test.mjs
    python site/hamingjur-docs/deploy.py --token-file ~/token.txt

For a token restricted to naamah's network, open the user's SSH SOCKS tunnel in
one terminal and deploy from another:

    ssh -N -D 127.0.0.1:1085 naamah

    python site/hamingjur-docs/deploy.py --token-file ~/token.txt \
        --socks-proxy 127.0.0.1:1085

The deployment script keeps the token in memory and passes it through curl's
stdin, not command arguments or persisted configuration. It refuses to replace
an unrelated hostname or Worker. Existing other-site bindings are not modified.
