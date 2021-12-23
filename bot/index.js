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

const DISCORD_MAX_MESSAGE_LENGTH = 2000;

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

		let sendChatChain = connected;

		let modified = false;
		let prevDirect, prevDirectContentReal, prevDirectOff;
		let prevPrompt, prevPromptContentReal, prevDisplay;
		const recvComm = async function(obj) {
			let editPrevMessage;
			if (obj.type === 'PROMPT') {
				let payload = obj.payload;
				// escape here: https://stackoverflow.com/a/39543625
				payload = payload.replace(/(\*|_|`|~|\\|>)/g, '\\$1');

				if (!payload.match(/^\s*$/))
					prevPrompt = await channel.send(payload);
				else
					prevPrompt = null;
				prevPromptContentReal = payload;
				prevDirect = prevDisplay = null;
			} else if (obj.type === 'DIRECT') {
				let payload = obj.payload;
				const lastCharIsLF = payload.charAt(payload.length - 1) === '\n';

				if (lastCharIsLF)
					payload = payload.substring(0, payload.length - 1);

				// NOTE: comm guarantees the payload won't have \r\n
				if (payload.charAt(0) === '\r') {
					prevDirectOff = 0;
					while (payload.charAt(0) === '\r')
						payload = payload.substring(1);
				}

				// escape here: https://stackoverflow.com/a/39543625
				payload = payload.replace(/(\*|_|`|~|\\|>)/g, '\\$1');

				let prevDirectOffFirstLine = null;
				if (prevDirect) {
					// Can't use prevDirect.content because spacing are trimmed
					let prevPayload = prevDirectContentReal;
					let prevLast, editPrev;

					// Split at first line in payload, first line should
					// go to editing last line of prev message, if possible
					const firstLF = payload.indexOf('\n');
					if (firstLF < 0) {
						editPrev = payload;
						payload = '';
					} else {
						editPrev = payload.substring(0, firstLF);
						payload = payload.substring(firstLF + 1);
					}

					const prevPayloadLastLF = prevPayload.lastIndexOf('\n');
					if (prevPayloadLastLF < 0) {
						prevLast = prevPayload;
						prevPayload = '';
					} else {
						prevLast = prevPayload.substring(prevPayloadLastLF + 1);
						prevPayload = prevPayload.substring(0, prevPayloadLastLF + 1);
					}

					let editPrevAgg = '';
					for (const [index, pseudo] of editPrev.split('\r').entries()) {
						if (index) {
							editPrevAgg = pseudo + editPrevAgg.substring(pseudo.length);
							prevDirectOff = pseudo.length;
						} else {
							editPrevAgg = prevLast.substring(0, prevDirectOff) +
								pseudo +
								prevLast.substring(prevDirectOff + pseudo.length);
							prevDirectOff += pseudo.length;
						}
					}
					editPrev = editPrevAgg;
					prevDirectOffFirstLine = prevDirectOff;

					if (firstLF < 0)
						payload = editPrev;
					else
						payload = editPrev + '\n' + payload;

					if (!prevPayload.match(/^\s*$/))
						await prevDirect.edit(prevPayload);
					else if (modified)
						await prevDirect.delete();
					else
						editPrevMessage = prevDirect;

					prevDirect = prevDirectContentReal = null;
				}

				if (payload.length) {
					const payloadLines = payload.split('\n');
					for (const [index, line] of payloadLines.entries()) {
						let aggline = '';
						for (const pseudo of line.split('\r')) {
							aggline = pseudo + aggline.substring(pseudo.length);
							prevDirectOff = pseudo.length;
						}

						if (!index && prevDirectOffFirstLine !== null)
							prevDirectOff = prevDirectOffFirstLine;

						payloadLines[index] = aggline;
					}

					payload = payloadLines.join('\n');
				}

				let lastMessage, lastMessagePayload;
				while (payload.length) {
					if (payload.length > DISCORD_MAX_MESSAGE_LENGTH) {
						let splitPoint = payload.lastIndexOf('\n', DISCORD_MAX_MESSAGE_LENGTH - 1);
						if (splitPoint < 0)
							splitPoint = DISCORD_MAX_MESSAGE_LENGTH;

						lastMessagePayload = payload.substring(0, splitPoint);
						payload = payload.substring(splitPoint);
					} else {
						lastMessagePayload = payload;
						payload = '';
					}

					if (!editPrevMessage) {
						if (!lastMessagePayload.match(/^\s*$/))
							lastMessage = await channel.send(lastMessagePayload);
						else
							lastMessage = null;
					} else {
						if (!lastMessagePayload.match(/^\s*$/)) {
							lastMessage = await editPrevMessage.edit(lastMessagePayload);
						} else {
							await editPrevMessage.delete();
							lastMessage = null;
						}

						editPrevMessage = null;
					}
				}

				prevDirect = lastCharIsLF ? null : lastMessage;
				prevDirectContentReal = lastCharIsLF ? null : lastMessagePayload;
				prevPrompt = prevDisplay = null;
			} else if (obj.type === 'DISPLAY') {
				const payload = '```\n' + obj.payload + '\n```';

				if (prevDisplay)
					prevDisplay = await prevDisplay.edit(payload);
				else
					prevDisplay = await channel.send(payload);

				prevPrompt = prevDirect = null;
			}

			modified = false;
		};

		// This promise chaining here is needed here because we need to
		// serialize when a drain is needed.
		let sendCommChain = connected;
		const sendComm = function(obj) {
			const pkt = JSON.stringify(obj);

			sendCommChain = sendCommChain.then(async function() {
				if (destroying)
					return;

				if (!comm.write(pkt + '\n'))
					await events.once(comm, 'drain');
			});
		};

		// Exported properties and methods
		const session = {
			_markModified: function() {
				modified = true;
			},
			message: function(message) {
				sendChatChain = sendChatChain.then(async () => {
					await connected;
					sendComm({ type: 'INPUT', payload: message + '\n' });

					if (prevPrompt) {
						await prevPrompt.delete();
						prevPrompt = null;
						await channel.send(prevPromptContentReal + message);
					}
				});
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
					sendChatChain = sendChatChain.then(async () => recvComm(obj));
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

		session._markModified();

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

		session._markModified();

		if (interaction.commandName === 'killall') {
			await interaction.reply('Destroying and restarting session...');
			session.killall();
		}
	});
}());
