import {translations} from './translations.js';
import {controlsTranslations} from './controls-translations.js';
import {backendTranslations} from './backend-translations.js';

const messages = Object.freeze(Object.assign(Object.create(null), translations, controlsTranslations, backendTranslations));
const reverse = new Map(Object.entries(messages).map(([pl, en]) => [en, pl]));
const placeholder = /\{([a-zA-Z][\w]*)\}/g;
const escapePattern = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function messagePattern(source, target) {
  const names = [...source.matchAll(placeholder)].map(match => match[1]);
  const parts = source.split(placeholder);
  const expression = parts.map((part, index) => index % 2 ? '([\\s\\S]+?)' : escapePattern(part)).join('');
  return {expression: new RegExp('^' + expression + '$'), target, names};
}
const patterns = Object.entries(messages).filter(([pl]) => /\{[a-zA-Z]/.test(pl)).map(([pl, en]) => ({
  en: messagePattern(pl, en), pl: messagePattern(en, pl)
}));
const listeners = new Set();
let selected = 'pl';
try {
  const saved = localStorage.getItem('3api-language');
  if (saved === 'pl' || saved === 'en') selected = saved;
} catch { /* The interface still works when browser storage is unavailable. */ }

export const language = () => selected;
export const locale = () => selected === 'en' ? 'en-GB' : 'pl-PL';
export function t(source, params = {}) {
  const value = selected === 'en' ? (messages[source] ?? source) : source;
  return String(value ?? '').replace(/\{([a-zA-Z][\w]*)\}/g, (match, key) =>
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : match);
}

// Use only for backend-owned status/error labels, never for user or model content.
export function translateMessage(value) {
  const source = String(value ?? '');
  const exact = selected === 'en' ? messages[source] : reverse.get(source);
  if (exact !== undefined) return exact;
  for (const pattern of patterns) {
    const {expression, target, names} = pattern[selected];
    const match = source.match(expression);
    if (match) return target.replace(placeholder, (token, name) => {
      const index = names.indexOf(name);
      return index >= 0 ? match[index + 1] : token;
    });
  }
  return source;
}

export function translateDom(root = document) {
  const select = selector => [
    ...(root.matches?.(selector) ? [root] : []), ...root.querySelectorAll(selector)
  ];
  for (const element of select('[data-i18n]')) element.textContent = t(element.dataset.i18n);
  for (const attribute of ['aria-label', 'placeholder', 'title', 'content']) {
    for (const element of select(`[data-i18n-${attribute}]`)) {
      element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`)));
    }
  }
}

export function onLanguageChange(callback) {
  listeners.add(callback);
  return () => listeners.delete(callback);
}

export function setLanguage(next) {
  if (!['pl', 'en'].includes(next)) return;
  selected = next;
  document.documentElement.lang = next;
  try { localStorage.setItem('3api-language', next); } catch {}
  translateDom();
  const picker = document.querySelector('#language-picker');
  if (picker) picker.value = next;
  for (const callback of listeners) callback(next);
}

export function initI18n() {
  setLanguage(selected);
  document.querySelector('#language-picker')?.addEventListener('change', event => setLanguage(event.target.value));
}
