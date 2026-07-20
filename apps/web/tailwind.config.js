/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./static/index.html", "./static/app.js"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        primary: "#3d6b8a",
        "primary-soft": "#5a8fb0",
        secondary: "#6b7a8a",
        surface: "#eceef1",
        "surface-dim": "#e2e4e8",
        "on-background": "#1a1c1e",
        "on-surface-variant": "#4a5560",
        "outline-variant": "#c8cdd4",
        background: "#e8eaed",
        error: "#ba1a1a",
        "error-container": "#ffdad6",
        "on-error-container": "#93000a",
        "inverse-surface": "#233144",
        "inverse-on-surface": "#eaf1ff",
        "secondary-fixed": "#71f8e4",
        "tertiary-fixed-dim": "#ffb86e",
        "on-surface": "#1a1c1e",
      },
      borderRadius: {
        "4xl": "2rem",
        "5xl": "2.5rem",
      },
      spacing: {
        "section-padding": "80px",
        "container-max": "1120px",
        "margin-mobile": "20px",
        gutter: "24px",
      },
      fontFamily: {
        sans: ["Inter", "PingFang SC", "Helvetica Neue", "sans-serif"],
      },
      fontSize: {
        "headline-md": ["24px", { lineHeight: "1.3", fontWeight: "600" }],
        "body-lg": ["18px", { lineHeight: "1.65", fontWeight: "400" }],
        "body-md": ["16px", { lineHeight: "1.55", fontWeight: "400" }],
        "headline-xl-mobile": [
          "32px",
          { lineHeight: "1.2", letterSpacing: "-0.02em", fontWeight: "700" },
        ],
        "headline-xl": [
          "46px",
          { lineHeight: "1.1", letterSpacing: "-0.02em", fontWeight: "700" },
        ],
        "label-md": [
          "13px",
          { lineHeight: "1", letterSpacing: "0.06em", fontWeight: "600" },
        ],
        "headline-lg": [
          "30px",
          { lineHeight: "1.2", letterSpacing: "-0.01em", fontWeight: "600" },
        ],
      },
    },
  },
  plugins: [],
};
