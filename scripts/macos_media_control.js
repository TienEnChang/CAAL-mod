ObjC.import('Foundation');

const MEDIA_REMOTE_PATH =
  '/System/Library/PrivateFrameworks/MediaRemote.framework/';
function json(value) {
  return JSON.stringify(value);
}

function run(argv) {
  const mediaRemote = $.NSBundle.bundleWithPath(MEDIA_REMOTE_PATH);
  if (!mediaRemote.load) {
    throw new Error('Unable to load the macOS MediaRemote framework');
  }

  const action = argv[0];
  if (action !== 'toggle') throw new Error(`Unsupported media action: ${action}`);

  const controllerClass = $.NSClassFromString('MRNowPlayingController');
  const controller = controllerClass.localRouteController;
  const options = $.NSDictionary.alloc.init;
  controller.sendCommandOptionsCompletion(2, options, null);

  // MediaRemote dispatches asynchronously. Keep osascript alive long enough for
  // mediaremoted to deliver the command before this process tears down its run loop.
  delay(0.5);
  return json({ ok: true });
}
