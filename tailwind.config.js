/** @type {import('tailwindcss').Config} */
//
// Static Tailwind build. The site used to load <script src="https://cdn.tailwindcss.com">
// -- the Play CDN -- on every page: ~400 KB of render-blocking JavaScript that
// compiles the stylesheet in the visitor's browser on each load, flashes
// unstyled content first, and is documented by Tailwind as not for production.
// Build the sheet once and serve it from /static instead:
//
//     npm run css          # writes static/css/tailwind.css (minified)
//     npm run css:watch    # rebuild on save while editing templates
//
// The built file IS committed: the Docker image has no node, so the build
// cannot run on Railway. Rebuild and commit whenever a template gains a class
// the sheet does not yet contain (a missing class shows up as unstyled).
module.exports = {
  content: [
    "./templates/**/*.html",
    // Several modules return HTML straight from Python (inline markup, class
    // strings assembled in f-strings, site_theme decor), so scan them too.
    "./*.py",
    "./static/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
