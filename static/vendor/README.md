# Local vendor assets

These files replace runtime CDN dependencies for the v5 app shell.

- `marked.min.js`: local copy of `marked@12.0.2`.
- `purify.min.js`: local copy of `dompurify@3.1.6`.
- `lucide.min.js`: local copy of the Lucide UMD browser bundle.
- `three.module.min.js` and `three.core.min.js`: the matching official ESM
  build pair from `three@0.185.1`. They are not part of the initial app shell;
  `topology-stage.js` imports them only after a user opens an optional 3D map.
  SHA-256: `86bcee248b64f44bcfc23c331ae74619061957d59cab040171dcb6fb5900beb6`
  (module) and
  `05b2609338c76cd65daf74f3ac515bc9a5045e1b3b33edc07d8c9bd55250fa90`
  (core).

Tailwind is compiled into `../app.css` from `../../tailwind.config.js` and
`../src/tailwind.css`; the old browser runtime is no longer loaded by the app.
