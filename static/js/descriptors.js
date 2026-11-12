// Alpine component for the Films tab descriptor filter.
// Pick a descriptor from the select, press +, and it accumulates as a chip.
// Every change submits the form; htmx swaps the library region in place.
function descriptorPicker() {
  return {
    selected: [],

    init() {
      const state = document.getElementById("descriptor-state");
      if (state) {
        this.selected = JSON.parse(state.textContent).selected;
      }
      // htmx replaces the region after each change, so the rebuilt picker
      // takes focus back when the swap was triggered from this component.
      if (window.__descriptorFocus) {
        window.__descriptorFocus = false;
        this.$nextTick(() => this.$refs.select.focus());
      }
    },

    add() {
      const name = this.$refs.select.value;
      if (name && !this.selected.includes(name)) {
        this.selected.push(name);
        this.submit();
      }
    },

    remove(name) {
      this.selected = this.selected.filter((value) => value !== name);
      this.submit();
    },

    submit() {
      // Alpine updates the DOM (hidden inputs, chips) on the next tick;
      // submitting sooner would send the previous selection.
      this.$nextTick(() => {
        window.__descriptorFocus = true;
        this.$refs.form.requestSubmit();
      });
    },
  };
}
