const moduleFiles = ['inputs.py', 'comparison.py', 'model.py', 'browser.py'];

export async function createCalculator(loadText = async url => {
  const result = await fetch(url);
  if (!result.ok) throw new Error('The pricing engine could not be loaded.');
  return result.text();
}) {
  const index = new URL('./pyodide/', import.meta.url);
  const { loadPyodide } = await import(new URL('pyodide.mjs', index));
  const runtime = await loadPyodide({
    stdout: () => {},
    stderr: () => {}
  });
  await runtime.loadPackage(['pyarrow', 'scipy', 'tzdata'], {
    messageCallback: () => {},
    errorCallback: () => {}
  });
  const sources = await Promise.all(moduleFiles.map(name => loadText(new URL(name, import.meta.url))));
  for (let i = 0; i < moduleFiles.length; i++) {
    runtime.FS.writeFile('/home/pyodide/' + moduleFiles[i], sources[i]);
  }
  runtime.runPython('from browser import receive_files, run_action');
  const receiveFiles = runtime.globals.get('receive_files');
  const runAction = runtime.globals.get('run_action');

  return {
    async upload(files) {
      if (!files.spx?.size || !files.es?.size) {
        throw new Error('Select an SPX Parquet and an ES options CSV.');
      }
      const slots = [files.spx, files.es, files.futures];
      if (slots.reduce((sum, file) => sum + (file?.size || 0), 0) > 75 * 1024 * 1024) {
        throw new Error('Files must be at most 75 MB combined.');
      }
      const payload = await Promise.all(slots.map(async file => file?.size
        ? new Uint8Array(await file.arrayBuffer()) : null));
      return JSON.parse(receiveFiles(payload, slots.map(file => file?.size ? file.name : null)));
    },
    run(action, payload) {
      return JSON.parse(runAction(action, JSON.stringify(payload)));
    }
  };
}
