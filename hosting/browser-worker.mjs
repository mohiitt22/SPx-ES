import { createCalculator } from './runtime.mjs';

let engine;
let queue = Promise.resolve();
self.onmessage = ({ data: { id, action, payload } }) => {
  queue = queue.then(async () => {
    try {
      engine ??= await createCalculator();
      const result = action === 'upload' ? await engine.upload(payload) : engine.run(action, payload);
      self.postMessage({ id, result });
    } catch (error) {
      // Python exceptions include a traceback. Show the final message to the reader.
      const message = String(error.message || error).trim().split('\n').at(-1)
        .replace(/^(ValueError|KeyError|OSError|ArrowInvalid|ArrowNotImplementedError|JsException):\s*/, '');
      self.postMessage({ id, error: message });
    }
  });
};
