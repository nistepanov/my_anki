/* The card's own maturity, for the badge row.

   AnkiDroid is the only client that hands scheduling data to a template; on iOS, desktop and
   in the preview the chip stays hidden rather than showing a guess dressed up as a fact. */
(function () {
  'use strict';

  // Anki's own boundary between a young and a mature card, so the chip agrees with the deck
  // statistics instead of inventing a second definition of maturity.
  var MATURE_INTERVAL_DAYS = 21;
  var CARD_TYPE_NEW = 0;
  var CARD_TYPE_LEARNING = 1;
  var CARD_TYPE_RELEARNING = 3;
  // AnkiDroid refuses the API without a contact address for breaking-change notices.
  var API_CONTRACT = {version: '0.0.3', developer: 'anki-deck@localhost'};
  var LABELS = {
    'new': '%%new%%',
    learning: '%%learning%%',
    young: '%%young%%',
    mature: '%%mature%%',
    days: '%%days%%'
  };

  function stateOf(cardType, days) {
    if (cardType === CARD_TYPE_NEW) {
      return 'new';
    }
    if (cardType === CARD_TYPE_LEARNING || cardType === CARD_TYPE_RELEARNING) {
      return 'learning';
    }
    return days >= MATURE_INTERVAL_DAYS ? 'mature' : 'young';
  }

  // A negative interval counts seconds, which is a card still inside its first day.
  function daysOf(interval) {
    return interval > 0 ? interval : 0;
  }

  // The contract API answers with a success flag around the value; older builds hand back the
  // bare value, and reading that as an object would quietly report every card as new.
  function valueOf(answer, fallback) {
    if (answer === null || answer === undefined) {
      return fallback;
    }
    if (typeof answer === 'object') {
      return answer.success ? Number(answer.value) : fallback;
    }
    return Number(answer);
  }

  function label(state, days) {
    return days > 0 ? LABELS[state] + ' · ' + days + ' ' + LABELS.days : LABELS[state];
  }

  function fill() {
    // The back repeats the front's markup, so every chip on the page is filled rather than
    // the first one found.
    var chips = document.querySelectorAll('.badge-maturity');
    if (!chips.length || typeof AnkiDroidJS === 'undefined') {
      return Promise.resolve();
    }
    var api = new AnkiDroidJS(API_CONTRACT);
    return Promise.all([api.ankiGetCardType(), api.ankiGetCardInterval()]).then(function (answers) {
      var days = daysOf(valueOf(answers[1], 0));
      var state = stateOf(valueOf(answers[0], CARD_TYPE_NEW), days);
      Array.prototype.forEach.call(chips, function (chip) {
        chip.textContent = label(state, days);
        chip.setAttribute('data-state', state);
        chip.hidden = false;
      });
    });
  }

  // A card that cannot report its own state is not worth an error on the face of it.
  Promise.resolve().then(fill).catch(function () {});
})();
