// Tailwind v4 ships a dedicated PostCSS plugin and handles autoprefixing
// internally, so the postcss pipeline is just one entry now.
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
