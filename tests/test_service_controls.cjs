const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const { test } = require('node:test');

const template = readFileSync('overseer/templates/services.html', 'utf8');
const code = template.slice(template.indexOf('  function isActionAvailable('), template.indexOf('  function matchesStatus('));

function element(tag) {
  return {
    tag, dataset: {}, attributes: {}, children: [],
    setAttribute(key, value) { this.attributes[key] = value; },
    appendChild(child) { this.children.push(child); },
    addEventListener() {},
  };
}

function harness(request) {
  const service = { id: 'web-1', name: 'web', status: 'running' };
  const errors = [];
  let card;
  const context = vm.createContext({
    document: { createElement: element },
    window: { confirm: () => true, Overseer: {
      requestJson: request, serviceAnchor: name => name,
      showError: message => errors.push(message), showSuccess() {},
    } },
    statusClass: status => status, addTextRow() {}, renderPorts() {}, formatDateTime() {},
    load: async () => {},
    renderServices: () => { card = vm.runInContext('renderService(service, new Set())', context); },
    service,
  });
  vm.runInContext(code, context);
  context.renderServices();
  return {
    context, errors,
    render: context.renderServices,
    card: () => card,
    buttons: () => card.children.at(-1).children,
    act: type => vm.runInContext(`action('${type}', service.id, null, service.name)`, context),
  };
}

test('a rejected action restores valid controls and retains the error', async () => {
  const h = harness(async () => { throw new Error('Docker operation failed'); });
  await h.act('stop');
  assert.deepEqual(h.buttons().map(button => button.disabled), [true, false, false]);
  assert.deepEqual(h.errors, ['Docker operation failed']);
  assert.equal(h.card().attributes['aria-busy'], undefined);
});

test('pending actions survive replacement and reject conflicting requests', async () => {
  let resolve;
  let requests = 0;
  const h = harness(() => {
    requests++;
    return new Promise(done => { resolve = done; });
  });
  const pending = h.act('restart');
  h.render(); // Polling and filter changes both recreate cards.
  assert.equal(h.card().attributes['aria-busy'], 'true');
  assert.deepEqual(h.buttons().map(button => button.disabled), [true, true, true]);
  assert.equal(h.buttons()[1].textContent, 'Restarting…');
  await h.act('stop');
  assert.equal(requests, 1);
  resolve({ success: true });
  await pending;
  assert.deepEqual(h.buttons().map(button => button.disabled), [true, false, false]);
});

test('completion restores controls using the latest refreshed status', async () => {
  let resolve;
  const h = harness(() => new Promise(done => { resolve = done; }));
  const pending = h.act('stop');
  h.context.service.status = 'exited';
  h.render();
  assert.deepEqual(h.buttons().map(button => button.disabled), [true, true, true]);
  resolve({ success: true });
  await pending;
  assert.deepEqual(h.buttons().map(button => button.disabled), [false, true, true]);
});
