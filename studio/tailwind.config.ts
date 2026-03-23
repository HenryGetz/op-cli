import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}", "./lib/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        panel: "#121822",
        panelAlt: "#0d121b",
        accent: "#39d5b6",
        warn: "#ff5d6c",
        pass: "#63f58a"
      },
      boxShadow: {
        panel: "0 18px 50px rgba(0, 0, 0, 0.35)"
      }
    }
  },
  plugins: []
};

export default config;
