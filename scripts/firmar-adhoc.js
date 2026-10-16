// Firma ad-hoc del .app antes de armar el DMG y el zip.
//
// Sin cuenta de Apple Developer no hay certificado, pero en Apple Silicon
// macOS no ejecuta código sin firmar: la firma ad-hoc ("-") alcanza para que
// la app arranque. electron-builder 25 no la hace sola (identity "-" la
// saltea por no encontrar ese certificado en el llavero), así que va acá.
const { execFileSync } = require('child_process');
const path = require('path');

exports.default = async function firmarAdhoc(context) {
  if (context.electronPlatformName !== 'darwin') return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  execFileSync('codesign', ['--force', '--deep', '--sign', '-', app], { stdio: 'inherit' });
  execFileSync('codesign', ['--verify', '--deep', '--strict', app], { stdio: 'inherit' });
  console.log(`  • firma ad-hoc lista: ${app}`);
};
