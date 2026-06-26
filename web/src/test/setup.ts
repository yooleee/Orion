// =============================================================================
// web/src/test/setup.ts
// -----------------------------------------------------------------------------
// Responsible for: Vitest global setup — registers jest-dom matchers (toBeInTheDocument,
//                  etc.) and clears the DOM between tests.
// Role in project: Loaded via vite.config.ts `test.setupFiles` before every test file.
// =============================================================================

import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Unmount React trees between tests so the jsdom document does not leak across cases.
afterEach(() => cleanup());
