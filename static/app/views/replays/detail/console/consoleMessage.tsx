import {Fragment} from 'react';
import styled from '@emotion/styled';
import {sprintf, vsprintf} from 'sprintf-js';

import DateTime from 'sentry/components/dateTime';
import AnnotatedText from 'sentry/components/events/meta/annotatedText';
import {getMeta} from 'sentry/components/events/meta/metaProxy';
import {Hovercard} from 'sentry/components/hovercard';
import {IconClose, IconWarning} from 'sentry/icons';
import {t} from 'sentry/locale';
import space from 'sentry/styles/space';
import {BreadcrumbTypeDefault} from 'sentry/types/breadcrumbs';

interface MessageFormatterProps {
  breadcrumb: BreadcrumbTypeDefault;
}

/**
 * Attempt to stringify
 */
function renderString(arg: string | number | boolean | Object) {
  if (typeof arg !== 'object') {
    return arg;
  }

  try {
    return JSON.stringify(arg);
  } catch {
    return arg.toString();
  }
}

/**
 * Attempt to emulate the browser console as much as possible
 */
function MessageFormatter({breadcrumb}: MessageFormatterProps) {
  // Browser's console formatter only works on the first arg
  const [message, ...args] = breadcrumb.data?.arguments;

  const isMessageString = typeof message === 'string';

  // Assumption here is that Array types are == placeholders, which means that's the number of arguments we need
  const placeholders = isMessageString
    ? sprintf.parse(message).filter(parsed => Array.isArray(parsed))
    : [];

  // Retrieve message formatting args
  const messageArgs = args.slice(0, placeholders.length);

  // Attempt to stringify the rest of the args
  const restArgs = args.slice(placeholders.length).map(renderString);

  const formattedMessage = isMessageString
    ? vsprintf(message, messageArgs)
    : renderString(message);

  // TODO(replays): Add better support for AnnotatedText (e.g. we use message
  // args from breadcrumb.data.arguments and not breadcrumb.message directly)
  return (
    <AnnotatedText
      meta={getMeta(breadcrumb, 'message')}
      value={[formattedMessage, ...restArgs].join(' ')}
    />
  );
}

interface ConsoleMessageProps extends MessageFormatterProps {
  isLast: boolean;
  relativeTimestamp: string;
}
function ConsoleMessage({
  breadcrumb,
  isLast,
  relativeTimestamp = '',
}: ConsoleMessageProps) {
  const ICONS = {
    error: <IconClose isCircled size="xs" />,
    warning: <IconWarning size="xs" />,
  };

  return (
    <Fragment>
      <Icon isLast={isLast} level={breadcrumb.level}>
        {ICONS[breadcrumb.level]}
      </Icon>
      <Message isLast={isLast} level={breadcrumb.level}>
        <MessageFormatter breadcrumb={breadcrumb} />
      </Message>
      <ConsoleTimestamp isLast={isLast} level={breadcrumb.level}>
        <Hovercard body={`${t('Relative Time')}: ${relativeTimestamp}`}>
          <DateTime date={breadcrumb.timestamp} timeOnly />
        </Hovercard>
      </ConsoleTimestamp>
    </Fragment>
  );
}

const Common = styled('div')<{isLast: boolean; level: string}>`
  background-color: ${p =>
    ['warning', 'error'].includes(p.level)
      ? p.theme.alert[p.level].backgroundLight
      : 'inherit'};
  color: ${p =>
    ['warning', 'error'].includes(p.level)
      ? p.theme.alert[p.level].iconHoverColor
      : 'inherit'};
  ${p => (!p.isLast ? `border-bottom: 1px solid ${p.theme.innerBorder}` : '')};
`;

const ConsoleTimestamp = styled(Common)<{isLast: boolean; level: string}>`
  padding: ${space(1)};
  border-left: 1px solid ${p => p.theme.innerBorder};
`;

const Icon = styled(Common)`
  padding: ${space(0.5)} ${space(1)};
`;
const Message = styled(Common)`
  padding: ${space(0.25)} 0;
  white-space: pre-wrap;
  word-break: break-word;
`;

export default ConsoleMessage;
