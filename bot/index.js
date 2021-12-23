const events = require('events');
const net = require('net');

const { Client, Intents } = require('discord.js');
const { SlashCommandBuilder } = require('@discordjs/builders');
const { REST } = require('@discordjs/rest');
const { Routes } = require('discord-api-types/v9');

const discord = new Client({
	intents: [
		Intents.FLAGS.GUILDS,
		Intents.FLAGS.GUILD_MESSAGES,
	],
});

const config = require('./config.json');
try {
	Object.assign(config, require('./config.private.json'));
} catch (e) {
	console.warn('No config.private.json found');
}

const rest = new REST({ version: '9' }).setToken(config.token);

const commands = [
	new SlashCommandBuilder().setName('killall')
		.setDescription('Destroys the current session and restarts it.'),
].map(command => command.toJSON());


(async function() {
	discord.login(config.token);
	await events.once(discord, 'ready');
	discord.user.setActivity('bash $');

	const sessions = new Map();

	// This is the main session reactor for each channel,
	// handling channel messages, slash commands, and data from its comm.
	const handleChannel = function(channel) {
		let comm, connectedResolve;
		const connected = new Promise((resolve, reject) => {
			connectedResolve = resolve;
		});

		let destroying = false;
		const destroy = function() {
			if (!destroying) {
				destroying = true;
				handleChannel(channel);
				if (comm) comm.destroy();
			}
		};

		const recvComm = function(obj) {
			if (obj.type === 'PROMPT' || obj.type === 'DIRECT') {
				// TODO: Handle \r here
				channel.send(obj.payload);
			} else if (obj.type === 'DISPLAY') {
				// TODO
			}
		};

		// This promise chaining here is needed here because we need to
		// serialize when a drain is needed.
		let sendCommPromise = connected;
		const sendComm = function(obj) {
			const pkt = JSON.stringify(obj);

			sendCommPromise = sendCommPromise.then(async function() {
				if (destroying)
					return;

				if (!comm.write(pkt + '\n'))
					await events.once(comm, 'drain');
			});
		};

		// Exported properties and methods
		const session = {
			message: async function(message) {
				await connected;
				sendComm({ type: 'INPUT', payload: message + '\n' });
			},
			killall: function() {
				destroy();
			},
		};

		sessions.set(channel.id, session);

		connected.then(() => {
			let buf = '';
			comm.on('data', chunk => {
				buf += chunk;

				while (true) {
					const lineInd = buf.indexOf('\n');
					if (lineInd < 0)
						break;

					const pkt = buf.substring(0, lineInd);
					buf = buf.substring(lineInd + 1);

					const obj = JSON.parse(pkt);
					recvComm(obj);
				}
			});

			comm.on('close', () => destroy());
		});

		// Everything ready, now connect.
		(async function() {
			for (let i = 0; ; i++) {
				await new Promise(resolve => setTimeout(resolve, 1000));
				if (destroying)
					return;

				console.log(`Establishing comm for channel #${channel.name}, attempt ${i + 1}`);
				comm = new net.Socket();

				try {
					comm.connect(49813);
					await events.once(comm, 'ready');
				} catch (e) {
					comm.destroy();
					await new Promise(resolve => setTimeout(resolve, 4000));
					continue;
				}

				console.log(`Channel #${channel.name} ready!`);
				connectedResolve();
				break;
			}
		}());
	};

	for (const [guildId, channels] of Object.entries(config.channels)) {
		await rest.put(
			Routes.applicationGuildCommands(discord.user.id, guildId),
			// Routes.applicationCommands(discord.user.id),
			{ body: commands },
		);

		for (const channelId of channels)
			handleChannel(await discord.channels.fetch(channelId));
	}

	discord.on('messageCreate', async message => {
		const session = sessions.get(message.channelId);
		if (!session)
			return;

		if (message.author.bot)
			return;

		const content = message.cleanContent;

		if (content.charAt(0) === '!')
			session.message(message.cleanContent.substring(1));
	});

	discord.on('interactionCreate', async interaction => {
		if (!interaction.isCommand())
			return;

		const session = sessions.get(interaction.channelId);

		if (!session) {
			await interaction.reply('This channel is not handled.');
			return;
		}

		if (interaction.commandName === 'killall') {
			await interaction.reply('Destroying and restarting session...');
			session.killall();
		}
	});
}());
