import React, {useCallback, useContext, useEffect, useRef, useState} from 'react';
import {useTheme} from '@emotion/react';
import {Replayer, ReplayerEvents} from 'rrweb';

import type ReplayReader from 'sentry/utils/replays/replayReader';
import usePrevious from 'sentry/utils/usePrevious';

import HighlightReplayPlugin from './highlightReplayPlugin';
import useRAF from './useRAF';

type Dimensions = {height: number; width: number};
type RootElem = null | HTMLDivElement;

// Important: Don't allow context Consumers to access `Replayer` directly.
// It has state that, when changed, will not trigger a react render.
// Instead only expose methods that wrap `Replayer` and manage state.
type ReplayPlayerContextProps = {
  /**
   * The time, in milliseconds, where the user focus is.
   * The user focus can be reported by any collaborating object, usually on
   * hover.
   */
  currentHoverTime: undefined | number;

  /**
   * The current time of the video, in milliseconds
   * The value is updated on every animation frame, about every 16.6ms
   */
  currentTime: number;

  /**
   * Original dimensions in pixels of the captured browser window
   */
  dimensions: Dimensions;

  /**
   * Duration of the video, in miliseconds
   */
  duration: undefined | number;

  /**
   * The calculated speed of the player when fast-forwarding through idle moments in the video
   * The value is set to `0` when the video is not fast-forwarding
   * The speed is automatically determined by the length of each idle period
   */
  fastForwardSpeed: number;

  /**
   * Required to be called with a <div> Ref
   * Represents the location in the DOM where the iframe video should be mounted
   *
   * @param _root
   */
  initRoot: (root: RootElem) => void;

  /**
   * Set to true while the library is reconstructing the DOM
   */
  isBuffering: boolean;

  /**
   * Whether the video is currently playing
   */
  isPlaying: boolean;

  /**
   * Whether fast-forward mode is enabled if RRWeb detects idle moments in the video
   */
  isSkippingInactive: boolean;

  /**
   * The core replay data
   */
  replay: ReplayReader | null;

  /**
   * Set the currentHoverTime so collaborating components can highlight related
   * information
   */
  setCurrentHoverTime: (time: undefined | number) => void;

  /**
   * Jump the video to a specific time
   */
  setCurrentTime: (time: number) => void;

  /**
   * Set speed for normal playback
   */
  setSpeed: (speed: number) => void;

  /**
   * The speed for normal playback
   */
  speed: number;

  /**
   * Start or stop playback
   *
   * @param play
   */
  togglePlayPause: (play: boolean) => void;

  /**
   * Allow RRWeb to use Fast-Forward mode for idle moments in the video
   *
   * @param skip
   */
  toggleSkipInactive: (skip: boolean) => void;
};

const ReplayPlayerContext = React.createContext<ReplayPlayerContextProps>({
  currentHoverTime: undefined,
  currentTime: 0,
  dimensions: {height: 0, width: 0},
  duration: undefined,
  fastForwardSpeed: 0,
  initRoot: () => {},
  isBuffering: false,
  isPlaying: false,
  isSkippingInactive: false,
  replay: null,
  setCurrentHoverTime: () => {},
  setCurrentTime: () => {},
  setSpeed: () => {},
  speed: 1,
  togglePlayPause: () => {},
  toggleSkipInactive: () => {},
});

type Props = {
  children: React.ReactNode;
  replay: ReplayReader | null;

  /**
   * Time, in seconds, when the video should start
   */
  initialTimeOffset?: number;

  /**
   * Override return fields for testing
   */
  value?: Partial<ReplayPlayerContextProps>;
};

function useCurrentTime(callback: () => number) {
  const [currentTime, setCurrentTime] = useState(0);
  useRAF(() => setCurrentTime(callback));
  return currentTime;
}

export function Provider({children, replay, initialTimeOffset = 0, value = {}}: Props) {
  const events = replay?.getRRWebEvents();

  const theme = useTheme();
  const oldEvents = usePrevious(events);
  // Note we have to check this outside of hooks, see `usePrevious` comments
  const hasNewEvents = events !== oldEvents;
  const replayerRef = useRef<Replayer>(null);
  const [dimensions, setDimensions] = useState<Dimensions>({height: 0, width: 0});
  const [currentHoverTime, setCurrentHoverTime] = useState<undefined | number>();
  const [isPlaying, setIsPlaying] = useState(false);
  const [isSkippingInactive, setIsSkippingInactive] = useState(false);
  const [speed, setSpeedState] = useState(1);
  const [fastForwardSpeed, setFFSpeed] = useState(0);
  const [buffer, setBufferTime] = useState({target: -1, previous: -1});
  const playTimer = useRef<number | undefined>(undefined);

  const forceDimensions = (dimension: Dimensions) => {
    setDimensions(dimension);
  };
  const setPlayingFalse = () => {
    setIsPlaying(false);
  };
  const onFastForwardStart = (e: {speed: number}) => {
    setFFSpeed(e.speed);
  };
  const onFastForwardEnd = () => {
    setFFSpeed(0);
  };

  const initRoot = useCallback(
    (root: RootElem) => {
      if (events === undefined) {
        return;
      }

      if (root === null) {
        return;
      }

      if (replayerRef.current) {
        if (!hasNewEvents) {
          // Already have a player for these events, the parent node must've re-rendered
          return;
        }

        if (replayerRef.current.iframe.contentDocument?.body.childElementCount === 0) {
          // If this is true, then no need to clear old iframe as nothing was rendered
          return;
        }

        // We have new events, need to clear out the old iframe because a new
        // `Replayer` instance is about to be created
        while (root.firstChild) {
          root.removeChild(root.firstChild);
        }
      }

      const highlightReplayPlugin = new HighlightReplayPlugin();

      // eslint-disable-next-line no-new
      const inst = new Replayer(events, {
        root,
        blockClass: 'sr-block',
        // liveMode: false,
        // triggerFocus: false,
        mouseTail: {
          duration: 0.75 * 1000,
          lineCap: 'round',
          lineWidth: 2,
          strokeStyle: theme.purple200,
        },
        // unpackFn: _ => _,
        plugins: [highlightReplayPlugin],
      });

      // @ts-expect-error: rrweb types event handlers with `unknown` parameters
      inst.on(ReplayerEvents.Resize, forceDimensions);
      inst.on(ReplayerEvents.Finish, setPlayingFalse);
      // @ts-expect-error: rrweb types event handlers with `unknown` parameters
      inst.on(ReplayerEvents.SkipStart, onFastForwardStart);
      inst.on(ReplayerEvents.SkipEnd, onFastForwardEnd);

      // `.current` is marked as readonly, but it's safe to set the value from
      // inside a `useEffect` hook.
      // See: https://reactjs.org/docs/hooks-faq.html#is-there-something-like-instance-variables
      // @ts-expect-error
      replayerRef.current = inst;
    },
    [events, theme.purple200, hasNewEvents]
  );

  useEffect(() => {
    if (replayerRef.current && events) {
      initRoot(replayerRef.current.wrapper.parentElement as RootElem);
    }
  }, [initRoot, events]);

  const getCurrentTime = useCallback(
    () => (replayerRef.current ? Math.max(replayerRef.current.getCurrentTime(), 0) : 0),
    []
  );

  const setCurrentTime = useCallback(
    (requestedTimeMs: number) => {
      const replayer = replayerRef.current;
      if (!replayer) {
        return;
      }

      const maxTimeMs = replayerRef.current?.getMetaData().totalTime;
      const time = requestedTimeMs > maxTimeMs ? 0 : requestedTimeMs;

      // Sometimes rrweb doesn't get to the exact target time, as long as it has
      // changed away from the previous time then we can hide then buffering message.
      setBufferTime({target: time, previous: getCurrentTime()});

      // Clear previous timers. Without this (but with the setTimeout) multiple
      // requests to set the currentTime could finish out of order and cause jumping.
      if (playTimer.current) {
        window.clearTimeout(playTimer.current);
      }

      if (isPlaying) {
        playTimer.current = window.setTimeout(() => replayer.play(time), 0);
        setIsPlaying(true);
      } else {
        playTimer.current = window.setTimeout(() => replayer.pause(time), 0);
        setIsPlaying(false);
      }
    },
    [getCurrentTime, isPlaying]
  );

  const setSpeed = useCallback(
    (newSpeed: number) => {
      const replayer = replayerRef.current;
      if (!replayer) {
        return;
      }
      if (isPlaying) {
        replayer.pause();
        replayer.setConfig({speed: newSpeed});
        replayer.play(getCurrentTime());
      } else {
        replayer.setConfig({speed: newSpeed});
      }
      setSpeedState(newSpeed);
    },
    [getCurrentTime, isPlaying]
  );

  const togglePlayPause = useCallback(
    (play: boolean) => {
      const replayer = replayerRef.current;
      if (!replayer) {
        return;
      }

      if (play) {
        replayer.play(getCurrentTime());
      } else {
        replayer.pause(getCurrentTime());
      }
      setIsPlaying(play);
    },
    [getCurrentTime]
  );

  const toggleSkipInactive = useCallback((skip: boolean) => {
    const replayer = replayerRef.current;
    if (!replayer) {
      return;
    }
    if (skip !== replayer.config.skipInactive) {
      replayer.setConfig({skipInactive: skip});
    }
    setIsSkippingInactive(skip);
  }, []);

  // Only on pageload: set the initial playback timestamp
  useEffect(() => {
    if (initialTimeOffset && events && replayerRef.current) {
      setCurrentTime(initialTimeOffset * 1000);
    }
  }, [events, replayerRef.current]); // eslint-disable-line react-hooks/exhaustive-deps

  const currentPlayerTime = useCurrentTime(getCurrentTime);

  const [isBuffering, currentTime] =
    buffer.target !== -1 && buffer.previous === currentPlayerTime
      ? [true, buffer.target]
      : [false, currentPlayerTime];

  if (!isBuffering && buffer.target !== -1) {
    setBufferTime({target: -1, previous: -1});
  }

  const event = replay?.getEvent();
  const duration = event ? (event.endTimestamp - event.startTimestamp) * 1000 : undefined;

  return (
    <ReplayPlayerContext.Provider
      value={{
        currentHoverTime,
        currentTime,
        dimensions,
        duration,
        fastForwardSpeed,
        initRoot,
        isBuffering,
        isPlaying,
        isSkippingInactive,
        replay,
        setCurrentHoverTime,
        setCurrentTime,
        setSpeed,
        speed,
        togglePlayPause,
        toggleSkipInactive,
        ...value,
      }}
    >
      {children}
    </ReplayPlayerContext.Provider>
  );
}

export const useReplayContext = () => useContext(ReplayPlayerContext);
