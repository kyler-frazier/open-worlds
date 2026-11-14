// Flat config (eslint 9). `npm run lint` referenced eslint for a long time
// without it ever being installed or configured, so the script always failed --
// which is worse than no script, because it reads as coverage that is not there.
import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "eslint.config.js"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // The rule worth having here. Every modal drives its own fetch/poll from
      // useEffect, and a missing dependency is a stale closure: a form that
      // silently reads a value from a previous render. Type-checking cannot see
      // it and it is exactly the class of bug that survives review.
      "react-hooks/exhaustive-deps": "warn",

      // Vite's fast refresh only works when a module exports components alone.
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],

      // An unused name is usually a leftover from an edit; the underscore
      // prefix is the escape hatch for deliberately-ignored bindings.
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
);
