import sys
import os
import json
import argparse
import logging
import logging.config
import signal
import tweepy
import pika
from retrying import retry
from logging.config import fileConfig

fileConfig('logging.ini')
logger = logging.getLogger()


class TwitterListener(tweepy.StreamListener):

    def __init__(self, twitter_handler, rmq_credentials, queue):
        self.rmq_credentials = rmq_credentials
        self.queue = queue
        self.twitter_handler = twitter_handler

    def on_data(self, str_data):
        try:
            data = json.loads(str_data)
        except ValueError as e:
            logger.warning('Could not parse JSON data. %s' % (str(e),))
            return True

        twitter_handler = data['user']['screen_name']
        user_name = data['user']['name']
        img_url = self.__get_img_url(data)

        if not img_url:
            logger.debug('No picture found')
            return True

        if twitter_handler.lower() == self.twitter_handler.lower():
            logger.debug('Ignoring tweet by user "%s"' % (self.twitter_handler, ))
            return True

        logger.info('[Tweet] User: %s (@%s). Pic URL: %s' % (user_name, twitter_handler, img_url))

        message = {'twitter_handler': twitter_handler, 'user_name': user_name, 'img_url': img_url}
        self.__send_message_to_queue(message)
        logger.debug('Notification sent to queue: %s' % (self.queue,))
        return True

    def on_error(self, status):
        logger.error('Error from the Twitter feed: %s' % (status,))

    def __get_img_url(self, data):
        if 'extended_entities' not in data:
            return None
        if 'media' not in data['extended_entities']:
            return None
        media = data['extended_entities']['media']
        if media[0]['type'] != 'photo':
            return None
        else:
            return media[0]['media_url']

    @retry(wait_exponential_multiplier=10000, stop_max_attempt_number=3)
    def __send_message_to_queue(self, message):
        connection = pika.BlockingConnection(pika.ConnectionParameters(
            host=self.rmq_credentials['host'], port=self.rmq_credentials['port'],
            credentials=pika.PlainCredentials(self.rmq_credentials['user'], self.rmq_credentials['password'])))
        channel = connection.channel()
        channel.queue_declare(queue=self.queue, durable=True)
        channel.basic_publish(exchange='',
                              routing_key=self.queue,
                              body=json.dumps(message),
                              properties=pika.BasicProperties(
                                  delivery_mode=2,
                              ))
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description='MosaicMe Listener. Listens the Twitter feed of a selected hashtag, extracts the tweeted images, and sends a notification to a RabbitMQ queue.')
    parser.add_argument('-t', '--hashtag', help='List of comma-separated hashtags. Do not include the # sign. Overwritten by MOSAIC_LISTEN_HASHTAG environment variable if present.', required=False)
    parser.add_argument('-q', '--queue',
                        help='Queue name to send a message. Overwritten by MOSAIC_QUEUE environment variable if present.',
                        required=False)
    args = parser.parse_args()

    hashtags = os.getenv('MOSAIC_LISTEN_HASHTAG', args.hashtag)
    if not hashtags:
        print('Hashtag not provided.')
        sys.exit(1)
    hashtags = hashtags.split(",")
    hashtags = list(map(lambda x: '#'+x, hashtags))

    queue = os.getenv('MOSAIC_QUEUE', args.queue)
    if not queue:
        print('Queue not provided.')
        sys.exit(2)

    try:
        twitter_consumer_key = os.environ['TWITTER_CONSUMER_KEY']
        twitter_consumer_secret = os.environ['TWITTER_CONSUMER_SECRET']
        twitter_access_token = os.environ['TWITTER_ACCESS_TOKEN']
        twitter_access_token_secret = os.environ['TWITTER_ACCESS_TOKEN_SECRET']
        twitter_username = os.environ['TWITTER_USERNAME']

        rmq_host = os.getenv('RABBITMQ_HOST', 'rabbit')
        rmq_port = int(os.environ['RABBITMQ_PORT'])
        rmq_user = os.environ['RABBITMQ_USER']
        rmq_password = os.environ['RABBITMQ_PASSWORD']
    except KeyError as e:
        print('Could not obtain environment variable: %s' % (e,))
        sys.exit(4)
    except Exception as e:
        print('Error: %s' % (e,))
        sys.exit(5)

    logger.info('Twitter and RabbitMQ credentials loaded correctly from environment')
    logger.info('Checking connection with RabbitMQ (%s:%s)...' % (rmq_host, rmq_port))

    try:
        connection = pika.BlockingConnection(pika.ConnectionParameters(
            host=rmq_host, port=rmq_port, credentials=pika.PlainCredentials(rmq_user, rmq_password)))
        connection.close()
    except Exception as e:
        print('Could not connect to RabbitMQ. %s' % (e,))
        sys.exit(7)

    logger.info('Connection with RabbitMQ verified successfully')

    rmq_credentials = {'host': rmq_host, 'port': rmq_port, 'user': rmq_user,
                       'password': rmq_password}

    l = TwitterListener(twitter_username, rmq_credentials, queue)
    auth = tweepy.OAuthHandler(twitter_consumer_key, twitter_consumer_secret)
    auth.set_access_token(twitter_access_token, twitter_access_token_secret)

    logger.info("Queue: %s" % (queue, ))
    logger.info('Listening to hashtags: %s' % (', '.join(hashtags),))

    stream = tweepy.Stream(auth, l)

    def signal_handler(signal, frame):
        logger.info('Stopping stream listener gracefully...')
        stream.disconnect()

    signal.signal(signal.SIGINT, signal_handler)
    stream.filter(track=hashtags)


if __name__ == "__main__":
    main()
