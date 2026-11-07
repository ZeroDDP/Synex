<!-- Оптимизировать svg иконки -->

<script>
  import { servers } from './stores.js';

  // проп закрытия экрана
  export let onClose;

  // локальное состояние экрана
  // Выбранный сервер слева (по умолчанию — первый из стора)
  let selectedId = '';

  // Поля формы
  let host = '';
  let port = '22';
  let username = '';
  let passphrase = '';
  let sshKey = '';

  // Режим авторизации: на макете активен SSH Key
  let authMode = 'key'; // 'password' | 'key'

  // Save server to list
  let saveToList = true;

  // bool для отображения ошибки Test connection
  let showError = false;

  // Строка поиска (Saved servers)
  let search = '';

  // Список слева (Поиск Saved servers)
  $: filtered = $servers.filter(
    (s) => s.name.toLowerCase().includes(search.toLowerCase()) ||
           s.host.includes(search)
  );

  // Заполнение формы при нажатии на сервер в списке
  function selectServer(s) {
    selectedId = s.id;
    host = s.host;
    username = 'admin';
    showError = false;
  }

  // Очистка формы при нажатии на add
  function startNew() {
    selectedId = '';
    host = '';
    port = '22';
    username = '';
    passphrase = '';
    sshKey = '';
    showError = false;
  }

  // Заглушка для демонстрации ошибки
  function testConnection() {
    showError = true;
  }

  // Заглушка подключения от кн. connect
  function connect() {
    // TODO: интеграция с бэкендом
    console.log('connect', { host, port, username, authMode, saveToList });
  }
</script>

<div class="screen">
  <!-- левая панель -->

  <section class="panel saved">
    <header class="saved__head">
      <span class="saved__title">SAVED SERVERS</span>
      <button type="button" class="saved__add" on:click={startNew}>+ Add</button>
    </header>

    <input
      class="search"
      type="text"
      placeholder="Saved servers"
      bind:value={search}
    />

    <ul class="saved__list">
      {#each filtered as s (s.id)}
        <li>
          <button
            type="button"
            class="srv"
            class:srv--active={s.id === selectedId}
            on:click={() => selectServer(s)}
          >
            <span class="dot" data-status={s.status} aria-hidden="true"></span>
            <span class="srv__body">
              <span class="srv__name">{s.name}</span>
              <span class="srv__sub">admin@{s.host}</span>
            </span>
          </button>
        </li>
      {/each}
    </ul>
  </section>

   <!-- правая панель -->
  <section class="panel form">
    <h2 class="form__title">New connection</h2>
    <p class="form__sub">Connect over SSH to a remote server</p>

    {#if showError}
      <div class="banner" role="alert">

        <svg width="34" height="30" viewBox="0 0 65 57" fill="none" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
        <path d="M10 0.5H54.8395C60.0862 0.5 64.3395 4.75329 64.3395 10V46.175C64.3395 51.4217 60.0862 55.675 54.8395 55.675H10C4.75329 55.675 0.5 51.4217 0.5 46.175V10C0.5 4.75329 4.75329 0.5 10 0.5Z" fill="#AA2E2E" fill-opacity="0.49" stroke="#AA2E2E"/>
        <path d="M28.8233 13.923L15.4258 36.7342C14.2512 38.7342 15.6933 41.2535 18.0126 41.2535H45.672C48.0285 41.2535 49.465 38.6613 48.2157 36.6632L33.9539 13.852C32.7595 11.9417 29.9643 11.9804 28.8233 13.923Z" fill="#AA2E2E" stroke-width="4" stroke-linecap="round" stroke="#AA2E2E"/>
        <use xlink:href="#stroke0_79_134" fill="white"/>
        <mask id="stroke1_79_134" fill="white">
        <use xlink:href="#stroke0_79_134"/>
        </mask>
        <path d="M34.3847 38.6203C34.3847 40.0746 33.0652 41.2535 31.4375 41.2535C29.8098 41.2535 28.4902 40.0746 28.4902 38.6203C28.4902 37.166 29.8098 35.9871 31.4375 35.9871C33.0652 35.9871 34.3847 37.166 34.3847 38.6203Z" stroke-width="2" stroke="#AA2E2E" mask="url(#stroke1_79_134)"/>
        <path d="M31.3296 22.8923V33.5674" stroke-width="4" stroke-linecap="round" stroke="white"/>
        <defs>
        <path id="stroke0_79_134" d="M34.3847 38.6203C34.3847 40.0746 33.0652 41.2535 31.4375 41.2535C29.8098 41.2535 28.4902 40.0746 28.4902 38.6203C28.4902 37.166 29.8098 35.9871 31.4375 35.9871C33.0652 35.9871 34.3847 37.166 34.3847 38.6203Z"/>
        </defs>
        </svg>

        <!-- Текст ошибки -->
        <span class="banner__text">Couldn't connected. Host didn't resposed</span>
      </div>
    {/if}

    <div class="row row--split">
      <label class="field">
        <span class="field__label">Host</span>
        <input class="input" type="text" bind:value={host} />
      </label>
      <label class="field field--port">
        <span class="field__label">Port</span>
        <input class="input" type="number" bind:value={port} />
      </label>
    </div>

    <label class="field">
      <span class="field__label">Username</span>
      <input class="input" type="text" bind:value={username} />
    </label>

    <div class="field">
      <span class="field__label">Authentication</span>
      <div class="tabs">
        <button
          type="button"
          class="tab"
          class:tab--active={authMode === 'password'}
          on:click={() => (authMode = 'password')}
        >
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 30" width="14" height="21" fill="none">
        <path d="M2 13.104h16v13.923H2V13.104zM11.087 20.674c.123.231.405.335.63.202.454-.267.82-.637 1.06-1.073.306-.562.38-1.2.21-1.804-.17-.605-.575-1.14-1.145-1.513a3.39 3.39 0 0 0-1.981-.533 3.322 3.322 0 0 0-1.919.688c-.529.416-.877.98-.984 1.596a2.415 2.415 0 0 0 .396 1.78c.296.435.723.786 1.231 1.017.178.08.367-.058.367-.253a.77.77 0 0 0-.314-.293c-.312-.178-.494-.491-.696-.788-.242-.356-.321-1.018-.25-1.428.072-.41.417-.877.77-1.154.352-.277.925-.459 1.399-.477.474-.02 1.297.228 1.677.477.38.249.656.716.77 1.12.113.402-.053.946-.257 1.32-.188.344-.424.768-.793.971a.502.502 0 0 0-.171.145l.292 2.915a1.447 1.447 0 1 1-2.87-.088l.443-2.72" stroke="#ADB4BF"/>
        <path d="M2.969 12.445c0-14.983 14.482-13.614 13.474-1.765m0 0c-.035.41-.088.831-.161 1.265m.16-1.265C17.175.313 2.284-1.5 3.546 12.445m12.898-1.765c-.03.409-.082.83-.161 1.265m0 0c-.03.165-.064.331-.102.5" stroke="#ADB4BF" stroke-width="3" stroke-linecap="round"/>
        </svg>
        Password
        </button>
        <button
          type="button"
          class="tab"
          class:tab--active={authMode === 'key'}
          on:click={() => (authMode = 'key')}
        >
        <svg width="18" height="27" viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M9.93646 13.4143C10.0133 13.5926 10.2642 13.5649 10.3093 13.3761L10.5225 12.485C10.5527 12.3588 10.5359 12.2259 10.4805 12.1086C9.75372 10.5687 10.1389 9.77623 10.5693 8.03371C11.0161 6.22423 12.0991 4.63594 13.6204 3.55908C15.1417 2.48221 16.9996 1.98866 18.8547 2.16862C20.7099 2.34858 22.4383 3.19003 23.7242 4.53921C25.0102 5.8884 25.7677 7.65523 25.8584 9.51686C25.9492 11.3785 25.367 13.2106 24.2184 14.6785C23.0698 16.1463 21.4314 17.1518 19.6025 17.5114C17.8831 17.8494 15.6073 18.1673 13.9959 17.4364C13.8594 17.3745 13.6965 17.4701 13.6965 17.62C15.5514 18.6786 17.7253 19.0344 19.8209 18.6224C21.9166 18.2105 23.794 17.0583 25.1102 15.3763C26.4264 13.6943 27.0934 11.5949 26.9894 9.46174C26.8855 7.32854 26.0174 5.30397 24.5439 3.75797C23.0704 2.21197 21.0898 1.24777 18.9641 1.04156C16.8383 0.83535 14.7093 1.40089 12.9662 2.63485C11.223 3.86881 9.98198 5.68879 9.46994 7.76223C9.00336 9.65161 9.17044 11.6365 9.93646 13.4143ZM13.6965 17.62L11.9377 19.7544C11.7332 20.0026 11.4082 20.1175 11.0931 20.0529C10.5193 19.9353 9.99602 20.4078 10.0546 20.9906L10.0886 21.3283C10.1326 21.7655 9.73944 22.1198 9.3091 22.0306L9.26498 22.0215C8.88384 21.9425 8.52632 22.2335 8.52632 22.6228C8.52632 22.9619 8.25139 23.2368 7.91225 23.2368H7.50001C6.93319 23.2368 6.47369 23.6963 6.47369 24.2632V24.9302C6.47369 25.3793 6.29529 25.81 5.97772 26.1275L5.78948 26.3158C5.35139 26.7539 4.75721 27 4.13765 27H3.39474C2.07216 27 1 25.9278 1 24.6053M9.93646 13.4143L2.70667 20.5306C1.61484 21.6053 1 23.0733 1 24.6053M1 24.6053L9.46994 17.2838" stroke-width="2" stroke="#ADB4BF"/>
        <path d="M20.7762 6.47368C20.7762 7.19311 20.193 7.77631 19.4735 7.77631C18.7541 7.77631 18.1709 7.19311 18.1709 6.47368C18.1709 5.75426 18.7541 5.17105 19.4735 5.17105C20.193 5.17105 20.7762 5.75426 20.7762 6.47368Z" stroke-width="1.5" stroke="#ADB4BF"/>
        </svg>
          SSH Key
        </button>
      </div>
    </div>

    {#if authMode === 'password'}
      <label class="field">
        <span class="field__label">Password</span>
        <input class="input" type="password" bind:value={passphrase} />
      </label>
    {:else}
      <label class="field">
        <span class="field__label">Passphrase(optional)</span>
        <input class="input" type="password" bind:value={passphrase} />
      </label>
      <label class="field">
        <span class="field__label">SSH Key</span>
        <input class="input" type="text" bind:value={sshKey} />
      </label>
    {/if}

    <label class="check">
      <input type="checkbox" bind:checked={saveToList} />
      <span class="check__box" aria-hidden="true">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 15" width="12" height="9" fill="none">
        <path d="M1 7.566l6.527 5.472L18.556 1" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </span>
      <span class="check__label">Save server to list</span>
    </label>

    <div class="actions">
      <button type="button" class="btn btn--primary" on:click={connect}>Connect</button>
      <button type="button" class="btn btn--outline" on:click={testConnection}>Test connection</button>
      <button type="button" class="btn btn--ghost" on:click={onClose}>Cancel</button>
    </div>
  </section>
</div>

<style>
  .screen {
    display: flex;
    gap: var(--synex-space-6);
    align-items: flex-start;
  }

  .panel {
    background: var(--synex-bg-app);
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
    padding: var(--synex-space-6);
  }

  /* левая панель */
  .saved { 
    width: 320px;
    flex: 0 0 auto;
    padding: var(--synex-space-4);
}

  .saved__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--synex-space-4);
  }
  .saved__title {
    font-family: var(--font-host);
    font-weight: 700;
    font-size: var(--font-size-14);
    color: var(--synex-text-muted);
  }

  .saved__add { 
    color: var(--synex-accent);
    font-size: var(--font-size-13);
    font-weight: 700; 
}

  .search {
    font-family: var(--font-host);
    width: 100%;
    font-size: var(--font-size-14);
    font-weight: 500;
    height: var(--synex-space-10);
    margin-bottom: var(--synex-space-5);
    padding: var(--synex-space-3);
    border: none;
    border-radius: var(--synex-radius-md);
    background: #d9d9d9;    
    color: #11131a;
  }
  .search::placeholder { color: #5b6472; }

  .saved__list {
    list-style: none;
    margin: var(--synex-space-4) 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--synex-space-4);
  }

  .srv {
    width: 100%;
    height: 80px;
    display: flex;
    align-items: center;
    gap: var(--synex-space-3);
    padding: var(--synex-space-4);
    border: 1px solid var(--synex-border-subtle);
    border-radius: var(--synex-radius-md);
    background: transparent;
    text-align: left;
    color: var(--synex-text-primary);
    font-family: var(--font-host);
  }
  .srv:hover { background: var(--synex-bg-hover); }
  .srv--active {
    background: var(--synex-bg-active);
    border-color: var(--synex-accent);
  }

  .dot {
    flex: 0 0 auto;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--synex-offline);
  }
  .dot[data-status='online']   { background: var(--synex-ok); }
  .dot[data-status='warning']  { background: var(--synex-warning); }
  .dot[data-status='critical'] { background: var(--synex-danger-fg); }

  .srv__body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .srv__name { font-size: var(--font-size-16); font-weight: 400; justify-content: center; }
  .srv__sub  { font-family: var(--font-mono); font-size: var(--font-size-9); color: var(--synex-text-muted); }

  /* ---- правая панель ---- */
  .form { flex: 1 1 auto; max-width: 400px; padding: var(--synex-space-4) }
  .form__title { margin: 0; font-size: var(--font-size-13); font-weight: 600; color: var(--synex-text-primary); font-family: var(--font-host); }
  .form__sub   { margin: 0 0 var(--synex-space-4); font-size: var(--font-size-12); color: var(--synex-text-secondary); font-family: var(--font-host);}

  .banner {
    display: flex;
    align-items: center;
    gap: var(--synex-space-3);
    padding: var(--synex-space-3) var(--synex-space-4);
    margin-bottom: var(--synex-space-4);
    border: 1px solid var(--synex-danger);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-danger-bg);
    color: var(--synex-danger-fg);
  }
  .banner__icon { flex: 0 0 auto; }
  .banner__text { font-size: 12px; }

  .row { display: flex; flex-direction: column; gap: var(--synex-space-2); }
  .row--split { flex-direction: row; gap: var(--synex-space-4); }
  .row--split .field { flex: 1 1 auto; }
  .field--port { flex: 0 0 120px !important; }

  .field { display: flex; flex-direction: column; gap: 2px; margin-bottom: var(--synex-space-4); }
  .field__label { font-size: 10px; color: var(--synex-text-secondary); font-family: var(--font-host);}

  .input {
    width: 100%;
    padding: var(--synex-space-3);
    border: none;
    border-radius: var(--synex-radius-sm);
    background: #d9d9d9;      
    color: #11131a;
    font: inherit;
  }

  .tabs { display: flex; gap: var(--synex-space-3); }
  .tab {
    flex: 1 1 0;
    font-size: var(--font-size-13);
    display: flex;
    align-items: center;
    /* justify-content: center; */
    gap: var(--synex-space-5);
    padding: var(--synex-space-4);
    border: 1px solid var(--synex-border-strong);
    border-radius: var(--synex-radius-sm);
    background: var(--synex-bg-hover);
    color: var(--synex-text-primary);
    font-weight: 600;
    height: 50px;
  }
  .tab--active {
    background: var(--synex-bg-active);
    border-color: var(--synex-accent);
  }
  .tab--active svg path {
    stroke:#FF7B00;
  }

  .check { display: flex; align-items: center; gap: var(--synex-space-2); margin-bottom: var(--synex-space-4); cursor: pointer; }
  .check input { position: absolute; opacity: 0; width: 0; height: 0; }
input[type="checkbox"] {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
}


/* Checkbox Save server to list */
input[type="checkbox"] {
  position: absolute;
  opacity: 0;
}
.check__box {
  width: var(--synex-space-4-5);
  height: var(--synex-space-4-5);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--synex-border-strong);
  border-radius: 4px;
}
.check__box svg {
  display: none;
  stroke: white;
}
input:checked + .check__box {
  background: var(--synex-brand-primary, #FF7B00);
  border-color: transparent;
}
input:checked + .check__box svg {
  display: block;
}




  .check input:checked + .check__box {
    background: var(--synex-accent);
    border-color: var(--synex-accent);
    color: var(--synex-accent-on);
  }
.check__label {
  font-size: 12px;
  color: var(--synex-text-secondary);
}

.actions {
  display: flex;
  gap: var(--synex-space-2);

}

.btn {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--synex-space-3) var(--synex-space-5);
  border-radius: var(--synex-radius-sm);
  font-weight: 600;
  font-size: var(--font-size-13);
  height: 40px;
  
}

.btn--primary {
  flex-grow: 1;
  background: var(--synex-accent);
  color: var(--synex-accent-on);
  font-family: var(--font-host);
}

.btn--primary:hover {
  background: var(--synex-accent-hover);
}

.btn--outline {
  flex-grow: 1.5;
  border: 1px solid var(--synex-border-default);
  color: var(--synex-text-primary);
  font-family: var(--font-host);
  background: rgba(80.66, 95.77, 119.51, 0.07);
}

.btn--outline:hover {
  background: var(--synex-bg-hover);
}

.btn--ghost {
  flex-grow: 1;
  color: var(--synex-text-primary);
  font-family: var(--font-host);
  border: 1px rgba(80.66, 95.77, 119.51, 0.15) solid;
}

.btn--ghost:hover {
  background: var(--synex-bg-hover);
}

</style>