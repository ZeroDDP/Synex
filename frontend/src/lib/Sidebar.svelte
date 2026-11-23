<!-- Единственный источнеик правды -->
<script>
  // Пропсы из App
  export let items;
  export let active;
  export let onSelect;
  export let alertCount = 0;
</script>

<aside class="sidebar">
  <header class="logo">
    <img class="logo-mark" src="/icons/synexlogo/Synexlogo.svg" alt="" />
    <span class="logo-text">SYNEX</span>
  </header>

  <nav class="nav" aria-label="Primary navigation">
    {#each items as item (item.id)}
      <button
        type="button"
        class="nav-btn"
        class:nav-btn--active={item.id === active}
        aria-current={item.id === active ? "page" : undefined}
        aria-label={item.id === "alerts" && alertCount > 0
          ? `${item.label}, ${alertCount} unacknowledged`
          : item.label}
        on:click={() => onSelect(item.id)}
      >
        <img class="nav-icon" src={item.icon} alt="" aria-hidden="true" />
        {item.label}
        {#if item.id === "alerts" && alertCount > 0}
          <span class="nav-badge">{alertCount > 99 ? "99+" : alertCount}</span>
        {/if}
      </button>
    {/each}
  </nav>

  <footer class="user">
    <span class="avatar" aria-hidden="true"></span>
    <span class="username">Username</span>
    <span class="dot" aria-hidden="true"></span>
  </footer>
</aside>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    flex: 0 0 var(--synex-sidebar-width);
    background: var(--synex-bg-surface);
    border-right: 1px solid var(--synex-border-subtle);
  }

  .logo {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 18px;
    padding: 0 20px;
    border-bottom: 1px solid var(--synex-border-subtle);
    height: 78px;
  }

  /* Заглушки для лого  */
  .logo-mark {
    display: block;
    width: 32px;
    height: 32px;
    object-fit: contain;
  }

  .logo-text {
    font-family: var(--font-inter);
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--synex-text-primary);
  }

  .nav {
    flex: 1 1 auto; /* Сайдбар растягивается по высоте */
    overflow-y: auto; /* Прокрутка сайдбара если не поместился текст */
    display: flex;
    flex-direction: column;
    gap: 0;
    padding: 14px 14px 14px 3px;
  }

  .nav-btn {
    display: flex;
    align-items: center;
    gap: var(--synex-space-6);
    padding: 0 16px 0 36px;
    margin-left: 7px;
    border: none;
    border-radius: var(--synex-radius-sm);
    background: transparent;
    color: var(--synex-accent);
    font-family: var(--font-inter);
    font-size: clamp(14px, calc(12px + 0.25vw), 18px);

    text-align: left;
    cursor: pointer;
    font-weight: 600;
    letter-spacing: 0.03em;
    height: clamp(48px, 4.2vw, 60px);
  }

  .nav-icon {
    display: block;
    flex: 0 0 34px;
    width: clamp(20px, 1.5vw, 25px);
    height: clamp(20px, 1.5vw, 25px);
    object-fit: contain;
  }

  .nav-badge {
    display: inline-flex;
    min-width: 20px;
    height: 20px;
    align-items: center;
    justify-content: center;
    margin-left: auto;
    padding: 0 5px;
    border-radius: 10px;
    background: var(--synex-danger);
    color: var(--synex-text-primary);
    font-size: 11px;
    font-weight: 700;
  }

  .nav-btn:hover {
    background: var(--synex-bg-hover);
  }

  .nav-btn--active {
    font-family: var(--font-inter);
    background: var(--synex-bg-active);
  }

  .user {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
    padding: 16px 26px;
    border-top: 1px solid var(--synex-border-subtle);
  }

  .avatar {
    flex: 0 0 auto;
    width: 50px;
    height: 50px;
    border-radius: 50%;
    background: var(--synex-border-strong);
  }

  .username {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 16px;
    color: var(--synex-text-primary);
    font-family: var(--font-host);
  }

  .dot {
    flex: 0 0 auto;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--synex-accent);
  }
</style>
