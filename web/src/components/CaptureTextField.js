import { TextField } from 'material/text/text-field.js';

// material's textarea field forces its internal resize grip on. Keep the
// existing M3 field, but disable that internal resizable mode for Capture.
class CaptureTextField extends TextField {
  updated(changedProperties) {
    super.updated(changedProperties);
    const field = this.renderRoot?.querySelector('md-field');
    if (field?.resizable) field.resizable = false;
  }
}

if (!customElements.get('capture-text-field')) {
  customElements.define('capture-text-field', CaptureTextField);
}
