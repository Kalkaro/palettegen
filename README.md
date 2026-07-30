# Stylix palette generator

A static, browser-only port of Stylix's image palette generator. Image decoding,
pixel sampling, LAB conversion, and the evolutionary palette search all happen
on the visitor's device. Generated images and palettes are stored locally in
IndexedDB.

## Run locally

The app uses a Web Worker, so serve the directory instead of opening the HTML as
a `file://` URL:

```sh
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

## Deploy to Vercel

Import the repository in Vercel and leave the framework preset as **Other**.
There is no build command and no output directory. `index.html` opens the
static app and `vercel.json` supplies its security headers.

The legacy Python/Nix server files remain in the working tree for reference but
are excluded from Vercel by `.vercelignore`; they are not used by the deployed
application.

## Browser behavior

- **generate** fetches a random, family-friendly image from the CORS-enabled
  nekos.best API.
- **upload** and drag-and-drop process a local JPEG, PNG, or WebP.
- The arrow button accepts direct HTTPS image URLs when that host permits CORS.
- History is private to the current browser and capped at 20 items.

The JavaScript port follows Stylix revision
`66714e5ce44269ecc58c20d9196da8dbe1b27a31`: its LAB conversions, palette
fitness function, 500 survivors, 50,000 population, 75% mutation probability,
and alternating crossover. It uses a seeded JavaScript PRNG and a streaming
top-k selection, so results follow the same algorithm but are not expected to
be byte-identical to the Haskell executable.
