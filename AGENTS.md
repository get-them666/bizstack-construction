# Master Architecture & Rules: BizStackPerks Ecosystem

## 1. Brand & Domain Mapping
- **Domain 1 (`bizstackperks.com`):** **Broom Service** (Short-Term Rental Cleaning & Co-Hosting). Phone: `+1 (757) 908-7121`. Features: Flat-Rate Turnover Calculator, Rental Analysis Report, Passwordless Client Portal, Worker/Cleaner Dispatch App, AI Voice Assistant.
- **Domain 2 (`construction.bizstackperks.com`):** **BuildStack Construction LLC** (General Contractor & STR Make-Ready). Phone: `+1 (757) 846-9275`, Email: `hello@bizstackperks.com`. Features: Free Estimate Form, Standard Scopes/Pricing, Client Portal, Owner/Admin Login, 24/7 AI Call/Text Assistant, Live Chat Widget.
- **Cross-Brand Loop:** Investors moving from a BuildStack renovation into Broom Service STR management must share unified customer/property records in the backend.

## 2. Strict Coding Rules
- **No Mock Data or Stubs:** Every form submission, calculator quote, chat message, email trigger, and voice webhook must hit real database tables and live API endpoints.
- **Isolation:** Fix one module at a time. Never rewrite working UI layouts when wiring backend logic.
- **Verification:** Run `npm run build` (or your project's typecheck/build script) after every phase. Do not mark a phase done until it compiles with zero errors.
