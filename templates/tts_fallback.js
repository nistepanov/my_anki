/* AnkiWeb has no text-to-speech and prints Anki's speech tag as plain text.

   A client that can speak replaces the tag before this runs, so this only touches a tag left
   as text: it becomes a button for the browser's own voice, or goes away when there is none. */
(function () {
  'use strict';

  var TAG_PATTERN = /\[anki:tts lang=([^\]]+)\]([\s\S]*?)\[\/anki:tts\]/;

  function speak(text, locale) {
    var utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = locale.replace('_', '-');
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  }

  function replaceTag(holder) {
    var match = TAG_PATTERN.exec(holder.textContent);
    if (match === null) {
      return;
    }
    holder.textContent = '';
    if (!('speechSynthesis' in window)) {
      return;
    }
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'tts-fallback-button';
    button.textContent = '🔊';
    button.addEventListener('click', function () {
      speak(match[2], match[1]);
    });
    holder.appendChild(button);
  }

  var holders = document.querySelectorAll('.tts-fallback');
  for (var index = 0; index < holders.length; index++) {
    replaceTag(holders[index]);
  }
})();
